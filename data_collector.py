"""
data_collector.py – Kickbase API & LigaInsider Scraping Pipeline

Verantwortlich für:
- Kickbase Login & Spieler-Abruf
- LigaInsider Scraping via Cloudscraper
- Fuzzy-Matching beider Datenquellen
- Strenge xP-Berechnung (nur verifizierte Starter)
"""

import os
import re
import requests
import pandas as pd
from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process
import cloudscraper
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KICKBASE_API_BASE = "https://api.kickbase.com/v4"
LIGAINSIDER_URL = "https://www.ligainsider.de/bundesliga/voraussichtliche-aufstellung/"
POSITION_MAP = {1: "TW", 2: "ABW", 3: "MIT", 4: "ST"}

HEADERS_API = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------
class KickbaseAPIError(Exception):
    """Fehler bei der Kommunikation mit der Kickbase API."""
    pass


class LigaInsiderError(Exception):
    """Fehler beim Auslesen von LigaInsider."""
    pass


# ---------------------------------------------------------------------------
# Kickbase API
# ---------------------------------------------------------------------------
def kickbase_login(email: str, password: str) -> dict:
    url = f"{KICKBASE_API_BASE}/user/login"
    payload = {"em": email, "pass": password, "loy": False, "rep": {}}

    try:
        resp = requests.post(url, json=payload, headers=HEADERS_API, timeout=15)
    except requests.RequestException as exc:
        raise KickbaseAPIError(f"Netzwerkfehler beim Login: {exc}") from exc

    if resp.status_code != 200:
        raise KickbaseAPIError(
            f"Login fehlgeschlagen (HTTP {resp.status_code}). Bitte E-Mail und Passwort prüfen."
        )

    data = resp.json()
    token = data.get("tkn") or data.get("token") or data.get("accessToken")
    if not token:
        raise KickbaseAPIError("Kein Auth-Token in der API-Antwort gefunden.")

    leagues = data.get("srvl", data.get("leagues", data.get("lg", [])))
    return {"token": token, "leagues": leagues}


def _get_auth_headers(token: str) -> dict:
    return {**HEADERS_API, "Authorization": f"Bearer {token}"}


def _parse_player_v4(p: dict, fallback_team: str = "") -> dict:
    name = p.get("n", "") or p.get("pn", "") or p.get("name", "")
    if not name:
        fn = p.get("fn", "") or p.get("firstName", "")
        ln = p.get("ln", "") or p.get("lastName", "")
        name = f"{fn} {ln}".strip()

    # Marktwert aus allen möglichen Kickbase-Feldern auslesen
    raw_mv = (
        p.get("marketValue")
        or p.get("mv")
        or p.get("price")
        or p.get("m")
        or p.get("market_value")
        or 0
    )

    # Position sicherstellen (1=TW, 2=ABW, 3=MIT, 4=ST)
    raw_pos = p.get("pos") or p.get("position") or p.get("p") or 1

    # Durchschnitts- und Gesamtpunkte
    raw_ap = p.get("ap") or p.get("averagePoints") or p.get("avgPoints") or p.get("points") or 0
    raw_tp = p.get("tp") or p.get("totalPoints") or 0

    # Status (0/fit, 1/verletzt, 2/gesperrt)
    raw_st = p.get("st", 0)
    if isinstance(raw_st, int):
        status = {0: "fit", 1: "verletzt", 2: "gesperrt"}.get(raw_st, "fit")
    elif isinstance(raw_st, str):
        status = raw_st.lower()
    else:
        status = "fit"

    return {
        "id": str(p.get("i", p.get("pi", p.get("id", "")))),
        "name": name,
        "team": p.get("tn", fallback_team) or str(p.get("tid", "")),
        "position": int(raw_pos),
        "market_value": float(raw_mv),
        "average_points": float(raw_ap),
        "total_points": float(raw_tp),
        "status": status,
    }


def fetch_kickbase_players(token: str, league_id: str) -> pd.DataFrame:
    """
    Lädt alle Bundesliga-Spieler sauber über die Team-Profile und mappt alle Felder.
    """
    auth_headers = _get_auth_headers(token)
    players = []

    # 1. Alle Bundesliga-Team-IDs holen
    team_ids = []
    try:
        url_ranking = f"{KICKBASE_API_BASE}/competitions/1/ranking"
        resp = requests.get(url_ranking, headers=auth_headers, timeout=15)
        if resp.status_code == 200:
            for team in resp.json().get("it", []):
                tid = team.get("tid", "")
                tn = team.get("tn", "")
                if tid:
                    team_ids.append((tid, tn))
    except requests.RequestException:
        pass

    # 2. Kader aller Teams laden
    for tid, tn in team_ids:
        try:
            url_team = f"{KICKBASE_API_BASE}/competitions/1/teams/{tid}/teamprofile"
            resp = requests.get(url_team, headers=auth_headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                team_name = data.get("tn", tn)
                for p in data.get("it", []):
                    players.append(_parse_player_v4(p, team_name))
        except requests.RequestException:
            continue

    if not players:
        raise KickbaseAPIError("Keine Spieler in Kickbase gefunden.")

    df = pd.DataFrame(players)
    df["position_label"] = df["position"].map(POSITION_MAP).fillna("MIT")
    return df


# ---------------------------------------------------------------------------
# LigaInsider Scraping – Voraussichtliche Aufstellung
# ---------------------------------------------------------------------------

# Bundesliga team slugs + LigaInsider IDs for direct access (fallback)
# IDs verified against ligainsider.de/bundesliga/voraussichtliche-aufstellung/
_TEAM_PAGES = [
    "fc-bayern-muenchen/1", "borussia-dortmund/14", "bayer-04-leverkusen/4",
    "rb-leipzig/1311", "eintracht-frankfurt/3", "vfb-stuttgart/12",
    "sc-freiburg/18", "borussia-moenchengladbach/5", "tsg-hoffenheim/10",
    "1-fc-union-berlin/1246", "sv-werder-bremen/2", "fc-augsburg/21",
    "1-fsv-mainz-05/17", "hamburger-sv/9", "1-fc-koeln/15",
    "fc-schalke-04/13",
]


def _create_scraper():
    """Create a cloudscraper instance that can bypass Cloudflare."""
    return cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "desktop": True,
        }
    )


def _extract_team_urls_from_overview(scraper) -> list[str]:
    """
    Scrape the overview page to get all team page URLs.
    Returns list of relative URLs like '/rb-leipzig/1311/'.
    """
    try:
        resp = scraper.get(LIGAINSIDER_URL, timeout=20)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Team page pattern: /team-slug/id/
            if re.match(r"^/[a-z0-9-]+/\d+/?$", href):
                urls.add(href.rstrip("/") + "/")
        return list(urls)
    except Exception:
        return []


def _scrape_team_lineup(scraper, team_url: str) -> list[dict]:
    """
    Scrape a single team's lineup page, e.g.
    https://www.ligainsider.de/rb-leipzig/1311/

    HTML structure (verified from raw HTML analysis):
    - The pitch visualization lives inside <div class="stadium_container_bg">
    - Each starter is in <div class="player_position_column"> with the name
      in <div class="player_name"><a itemprop="athlete">DisplayName</a></div>
    - After the pitch: "fehlen" (missing) section, then full squad list
    - We ONLY extract players from the pitch container to avoid false matches
    """
    full_url = f"https://www.ligainsider.de{team_url}"
    slug = team_url.strip("/").rsplit("/", 1)[0]
    team_name = slug.replace("-", " ").title()

    try:
        resp = scraper.get(full_url, timeout=30)
        if resp.status_code != 200:
            return []
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # === PRIMARY: Use the exact CSS selectors for the pitch section ===
    # The pitch container has class "stadium_container_bg"
    # Players are in <a itemprop="athlete"> inside <div class="player_name">
    pitch = soup.find("div", class_="stadium_container_bg")
    if pitch:
        starters = []
        seen = set()
        for a in pitch.find_all("a", attrs={"itemprop": "athlete"}):
            name = a.get_text(strip=True)
            href = a.get("href", "")
            if name and href and href not in seen:
                seen.add(href)
                starters.append({
                    "name": name,
                    "team": team_name,
                    "li_status": "Startelf",
                })
        return starters

    # === FALLBACK: Use player_name class within player_position_column ===
    columns = soup.find_all("div", class_="player_position_column")
    if columns:
        starters = []
        seen = set()
        for col in columns:
            name_div = col.find("div", class_="player_name")
            if name_div:
                a = name_div.find("a")
                if a:
                    name = a.get_text(strip=True)
                    href = a.get("href", "")
                    if name and href and href not in seen:
                        seen.add(href)
                        starters.append({
                            "name": name,
                            "team": team_name,
                            "li_status": "Startelf",
                        })
        return starters

    return []


def scrape_ligainsider() -> list[dict]:
    """
    Scrape the predicted starting XI from LigaInsider for all Bundesliga teams.

    Returns a list of dicts: [{name, team, li_status}, ...]
    Where li_status is 'Startelf' for predicted starters.
    """
    scraper = _create_scraper()

    # 1. Try to get team URLs from overview page
    team_urls = _extract_team_urls_from_overview(scraper)

    # 2. Fallback: use hardcoded team slugs
    if not team_urls:
        team_urls = [f"/{slug}/" for slug in _TEAM_PAGES]

    # 3. Scrape each team's lineup
    all_starters = []
    for url in team_urls:
        starters = _scrape_team_lineup(scraper, url)
        all_starters.extend(starters)

    return all_starters


def fuzzy_match_players(
    kb_players: pd.DataFrame,
    li_players: list[dict],
    threshold: int = 85,
) -> pd.DataFrame:
    """
    Match Kickbase players against the LigaInsider predicted starting XI.

    IMPORTANT: Only players who actually appear in the LigaInsider starting XI
    get p_start = 1.0. This replaces the old logic that gave all fit players
    with average_points >= 40 a guaranteed start.
    """
    # Build the set of LigaInsider starter names
    li_starter_names = [
        p["name"] for p in li_players if p.get("li_status") == "Startelf"
    ]

    matched_statuses = []
    for _, row in kb_players.iterrows():
        kb_name = row.get("name", "")
        kb_status = row.get("status", "fit")

        # 1. Kickbase status filter
        if kb_status != "fit":
            matched_statuses.append(("Kickbase: Nicht fit", 0.0))
            continue

        # 2. Check if player is in LigaInsider's predicted starting XI
        is_starter = False
        if li_starter_names:
            match = process.extractOne(
                kb_name, li_starter_names, scorer=fuzz.token_sort_ratio
            )
            if match and match[1] >= threshold:
                is_starter = True

        if is_starter:
            matched_statuses.append(("Startelf (LigaInsider)", 1.0))
        else:
            # Not in the predicted starting XI → no guaranteed start
            # Give a small probability for fit players with decent average
            avg_points = row.get("average_points", 0)
            if avg_points >= 50:
                matched_statuses.append(("Kein Starter lt. LigaInsider", 0.15))
            else:
                matched_statuses.append(("Kein Starter lt. LigaInsider", 0.0))

    kb_players["li_status"] = [s[0] for s in matched_statuses]
    kb_players["p_start"] = [s[1] for s in matched_statuses]
    return kb_players


def calculate_xp(players: pd.DataFrame) -> pd.DataFrame:
    """
    Berechnet xP tolerant, damit der ILP-Solver für alle Formationen genug Auswahl hat.
    """
    if "p_start" not in players.columns:
        players["p_start"] = 1.0

    players["xp"] = players["average_points"] * players["p_start"]

    # Filter: Nur verletzte/gesperrte Spieler ausschließen, Punkte über 0 bevorzugen
    mask = (
        (players["status"] == "fit")
        & (players["average_points"] > 0)
    )
    filtered = players[mask].copy()

    # Falls der Filter zu streng war, nimm alle fitten Spieler
    if len(filtered) < 15:
        filtered = players[players["status"] == "fit"].copy()

    filtered["id"] = filtered["id"].astype(str)
    return filtered.sort_values("xp", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Pipeline Entry Point
# ---------------------------------------------------------------------------
def run_pipeline(email: str, password: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    auth = kickbase_login(email, password)
    token = auth["token"]

    leagues = auth["leagues"]
    if not leagues:
        raise KickbaseAPIError("Keine Liga gefunden. Bist du einer Liga beigetreten?")

    first = leagues[0]
    league_id = str(first.get("i", first.get("id", first.get("leagueId", "")))) if isinstance(first, dict) else str(first)

    all_players = fetch_kickbase_players(token, league_id)

    try:
        li_players = scrape_ligainsider()
        all_players = fuzzy_match_players(all_players, li_players)
    except Exception as e:
        # IMPORTANT: Do NOT mark all fit players as starters on failure!
        # That was the old bug. Instead, mark them as unavailable.
        all_players["li_status"] = f"LigaInsider nicht verfügbar ({type(e).__name__})"
        all_players["p_start"] = 0.0

    optimizable = calculate_xp(all_players)
    return all_players, optimizable