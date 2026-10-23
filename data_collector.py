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


def _parse_player_v4(p: dict, fallback_team: str = "", team_map: dict[str, str] | None = None) -> dict:
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

    tid_str = str(p.get("tid", "")).strip()
    team_name = p.get("tn", "") or fallback_team
    if not team_name and team_map and tid_str in team_map:
        team_name = team_map[tid_str]
    if not team_name:
        team_name = tid_str

    return {
        "id": str(p.get("i", p.get("pi", p.get("id", "")))).strip(),
        "tid": tid_str,
        "name": name,
        "team": team_name,
        "position": int(raw_pos),
        "market_value": float(raw_mv),
        "average_points": float(raw_ap),
        "total_points": float(raw_tp),
        "status": status,
    }


def _get_active_challenge_ids(auth_headers: dict) -> list[str]:
    """
    Ermittelt aktive Kickbase Arena / Challenge IDs (bevorzugt 1. Bundesliga Matchday Challenge).
    """
    candidates: dict[str, int] = {}
    endpoints = [
        f"{KICKBASE_API_BASE}/challenges/selection",
        f"{KICKBASE_API_BASE}/challenges/lobby/overview",
        f"{KICKBASE_API_BASE}/challenges/overview",
        f"{KICKBASE_API_BASE}/challenges/recommended",
        f"{KICKBASE_API_BASE}/challenges/lobby/explore",
    ]

    for idx, url in enumerate(endpoints):
        try:
            resp = requests.get(url, headers=auth_headers, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            items = []
            for key in ("it", "my", "exp", "spo"):
                val = data.get(key)
                if isinstance(val, list):
                    items.extend(val)
            if isinstance(data.get("sch"), dict):
                items.append(data["sch"])

            for item in items:
                if not isinstance(item, dict):
                    continue
                ch_id = str(item.get("ch", item.get("i", item.get("id", "")))).strip()
                if not ch_id:
                    continue
                cpi = str(item.get("cpi", "")).strip()
                cht = item.get("cht", item.get("t", 0))
                cname = str(item.get("n", item.get("chn", ""))).lower()

                score = 10 - idx
                if cpi == "1" or ("bundesliga" in cname and "2." not in cname and "2-" not in cname):
                    score += 50
                if cht == 2:
                    score += 20

                if ch_id not in candidates or score > candidates[ch_id]:
                    candidates[ch_id] = score
        except requests.RequestException:
            continue

    return [cid for cid, _ in sorted(candidates.items(), key=lambda x: x[1], reverse=True)]


def _extract_player_list_from_payload(data: dict) -> list[dict]:
    """Extrahiert Spieler-Dicts aus verschiedenen Challenge-Response-Strukturen."""
    if not isinstance(data, dict):
        return []
    for key in ("it", "lp", "usfp", "pl", "players"):
        val = data.get(key)
        if isinstance(val, list) and val:
            # Prüfen ob es wirklich Spieler-Objekte sind (haben pi/i und mv/pos/n/pn)
            first = val[0]
            if isinstance(first, dict) and any(k in first for k in ("mv", "pos", "pi", "pn")):
                return val
    return []


def _fetch_arena_players_for_challenge(
    auth_headers: dict,
    challenge_id: str,
    team_map: dict[str, str],
) -> dict[str, dict]:
    """
    Lädt alle Spieler und deren spezifische Arena-Marktwerte für eine Challenge-ID
    über /v4/challenges/{challengeId}/lineup/overview und /lineup/selection.
    """
    arena_players: dict[str, dict] = {}
    rep_by_pos: dict[int, str] = {}

    # 1. Lineup-Overview abrufen (enthält bereits aufgestellte Spieler in 'lp')
    url_overview = f"{KICKBASE_API_BASE}/challenges/{challenge_id}/lineup/overview"
    try:
        resp = requests.get(url_overview, headers=auth_headers, timeout=12)
        if resp.status_code in (400, 403, 404):
            # Falls User der Challenge noch nicht beigetreten ist, Beitritt versuchen
            try:
                requests.post(
                    f"{KICKBASE_API_BASE}/challenges/{challenge_id}/join",
                    headers=auth_headers,
                    timeout=10,
                )
                requests.post(
                    f"{KICKBASE_API_BASE}/challenges/join",
                    json={"ch": str(challenge_id)},
                    headers=auth_headers,
                    timeout=10,
                )
                resp = requests.get(url_overview, headers=auth_headers, timeout=12)
            except requests.RequestException:
                pass

        if resp.status_code == 200:
            ov_data = resp.json()
            for raw_p in _extract_player_list_from_payload(ov_data):
                parsed = _parse_player_v4(raw_p, team_map=team_map)
                pid = parsed["id"]
                if pid:
                    arena_players[pid] = parsed
                    pos = parsed.get("position", 0)
                    if pos in (1, 2, 3, 4) and pos not in rep_by_pos:
                        rep_by_pos[pos] = pid
    except requests.RequestException:
        pass

    # 2. Alle wählbaren Challenge-Spieler über /lineup/selection abrufen
    url_selection = f"{KICKBASE_API_BASE}/challenges/{challenge_id}/lineup/selection"
    any_rep_id = next(iter(rep_by_pos.values()), None)

    for pos in (1, 2, 3, 4):
        # Strategien: erst ohne playerToBeReplacedById, dann (falls Aufstellung voll) mit playerToBeReplacedById
        param_variants = [{"position": str(pos)}]
        rep_id = rep_by_pos.get(pos) or any_rep_id
        if rep_id:
            param_variants.append({"position": str(pos), "playerToBeReplacedById": str(rep_id)})

        for base_params in param_variants:
            got_any_for_variant = False
            start = 0
            page_size = 250
            while start <= 500:
                params = {**base_params, "start": str(start), "max": str(page_size)}
                try:
                    resp = requests.get(url_selection, headers=auth_headers, params=params, timeout=12)
                    if resp.status_code != 200:
                        break
                    items = _extract_player_list_from_payload(resp.json())
                    if not items:
                        break

                    new_count = 0
                    for raw_p in items:
                        parsed = _parse_player_v4(raw_p, team_map=team_map)
                        pid = parsed["id"]
                        if pid and parsed["market_value"] > 0:
                            if pid not in arena_players:
                                new_count += 1
                            arena_players[pid] = parsed
                            got_any_for_variant = True

                    if len(items) < 20 or new_count == 0:
                        break
                    start += len(items)
                except requests.RequestException:
                    break

            if got_any_for_variant:
                break

    # 3. Fallback: unpositionierte Abfrage auf /lineup/selection, falls noch leer
    if len(arena_players) < 20:
        for extra_params in ({"start": "0", "max": "500"}, {}):
            try:
                resp = requests.get(url_selection, headers=auth_headers, params=extra_params, timeout=12)
                if resp.status_code == 200:
                    for raw_p in _extract_player_list_from_payload(resp.json()):
                        parsed = _parse_player_v4(raw_p, team_map=team_map)
                        if parsed["id"] and parsed["market_value"] > 0:
                            arena_players[parsed["id"]] = parsed
            except requests.RequestException:
                continue

    return arena_players


def fetch_kickbase_players(token: str, league_id: str = "") -> pd.DataFrame:
    """
    Lädt alle Bundesliga-Spieler und aktualisiert deren Marktwerte gezielt
    mit den offiziellen Arena-/Challenge-Marktwerten (aus /v4/challenges/...).
    """
    auth_headers = _get_auth_headers(token)
    players = []
    team_map: dict[str, str] = {}

    # 1. Alle Bundesliga-Team-IDs holen
    team_ids = []
    try:
        url_ranking = f"{KICKBASE_API_BASE}/competitions/1/ranking"
        resp = requests.get(url_ranking, headers=auth_headers, timeout=15)
        if resp.status_code == 200:
            for team in resp.json().get("it", []):
                tid = str(team.get("tid", "")).strip()
                tn = team.get("tn", "")
                if tid:
                    team_ids.append((tid, tn))
                    if tn:
                        team_map[tid] = tn
    except requests.RequestException:
        pass

    # 2. Basis-Kader aller Teams laden (enthält Vereinsnamen, Gesamtpunkte etc.)
    for tid, tn in team_ids:
        try:
            url_team = f"{KICKBASE_API_BASE}/competitions/1/teams/{tid}/teamprofile"
            resp = requests.get(url_team, headers=auth_headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                team_name = data.get("tn", tn)
                if tid and team_name:
                    team_map[tid] = team_name
                for p in data.get("it", []):
                    players.append(_parse_player_v4(p, team_name, team_map=team_map))
        except requests.RequestException:
            continue

    # 3. Arena-Modus (Challenge) Marktwerte abrufen und anwenden
    arena_players_by_id: dict[str, dict] = {}
    challenge_ids = _get_active_challenge_ids(auth_headers)
    for ch_id in challenge_ids:
        fetched = _fetch_arena_players_for_challenge(auth_headers, ch_id, team_map)
        if fetched:
            arena_players_by_id.update(fetched)
        if len(arena_players_by_id) >= 100:
            break

    # Zusätzlich Name-Lookup für Arena-Spieler aufbauen
    arena_players_by_name: dict[str, dict] = {
        ap["name"].strip().lower(): ap
        for ap in arena_players_by_id.values()
        if ap.get("name")
    }

    seen_ids = set()
    for p in players:
        pid = str(p["id"]).strip()
        seen_ids.add(pid)
        pname_key = str(p.get("name", "")).strip().lower()

        arena_p = arena_players_by_id.get(pid) or arena_players_by_name.get(pname_key)
        if arena_p and arena_p.get("market_value", 0) > 0:
            # Exakten Arena-Marktwert übernehmen
            p["market_value"] = float(arena_p["market_value"])
            if arena_p.get("average_points", 0) > 0:
                p["average_points"] = float(arena_p["average_points"])
        elif p.get("market_value", 0) > 0:
            # Im Arena-Modus sind Marktwerte auf glatte 100.000 € gerundet
            p["market_value"] = float(round(p["market_value"] / 100_000) * 100_000)

    # Falls im Arena-Endpunkt Spieler sind, die noch nicht in players waren, ergänzen
    for pid, arena_p in arena_players_by_id.items():
        if pid not in seen_ids and arena_p.get("name") and arena_p.get("market_value", 0) > 0:
            players.append(arena_p)
            seen_ids.add(pid)

    if not players:
        raise KickbaseAPIError("Keine Spieler in Kickbase gefunden.")

    df = pd.DataFrame(players)
    df["base_market_value"] = df["market_value"].astype(float)
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

    leagues = auth.get("leagues", [])
    league_id = ""
    if leagues:
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
    all_players["id"] = all_players["id"].astype(str).str.strip()
    return all_players, optimizable