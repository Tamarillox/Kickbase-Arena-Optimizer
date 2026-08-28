"""
kicktipp_engine.py – Offizieller Spielplan, historische Saisonergebnisse & LigaInsider-Live-Scan
"""

import requests
from bs4 import BeautifulSoup
import cloudscraper
import numpy as np

# Basis-Stärken & Offensiv-Faktoren der Bundesliga-Teams
TEAM_POWER_INDEX = {
    "FC Bayern München": {"offense": 95, "defense": 88, "overall": 92, "coach_pressure": 15},
    "Bayer 04 Leverkusen": {"offense": 90, "defense": 86, "overall": 89, "coach_pressure": 10},
    "Borussia Dortmund": {"offense": 88, "defense": 81, "overall": 85, "coach_pressure": 35},
    "RB Leipzig": {"offense": 86, "defense": 84, "overall": 85, "coach_pressure": 20},
    "VfB Stuttgart": {"offense": 84, "defense": 80, "overall": 82, "coach_pressure": 25},
    "Eintracht Frankfurt": {"offense": 82, "defense": 78, "overall": 80, "coach_pressure": 30},
    "SC Freiburg": {"offense": 78, "defense": 79, "overall": 78, "coach_pressure": 10},
    "TSG 1899 Hoffenheim": {"offense": 79, "defense": 74, "overall": 77, "coach_pressure": 55},
    "1. FSV Mainz 05": {"offense": 76, "defense": 76, "overall": 76, "coach_pressure": 45},
    "SV Werder Bremen": {"offense": 76, "defense": 74, "overall": 75, "coach_pressure": 40},
    "Borussia Mönchengladbach": {"offense": 77, "defense": 73, "overall": 75, "coach_pressure": 60},
    "1. FC Union Berlin": {"offense": 73, "defense": 77, "overall": 75, "coach_pressure": 45},
    "FC Augsburg": {"offense": 74, "defense": 73, "overall": 73, "coach_pressure": 65},
    "Hamburger SV": {"offense": 74, "defense": 72, "overall": 73, "coach_pressure": 50},
    "1. FC Köln": {"offense": 73, "defense": 72, "overall": 73, "coach_pressure": 55},
    "FC Schalke 04": {"offense": 72, "defense": 71, "overall": 72, "coach_pressure": 70},
    "SC Paderborn 07": {"offense": 71, "defense": 70, "overall": 71, "coach_pressure": 60},
    "SV Elversberg": {"offense": 70, "defense": 70, "overall": 70, "coach_pressure": 40},
}

# Eindeutige Aliase zur sicheren Erkennung (verhindert 'SV'-Konflikte)
TEAM_ALIASES = {
    "FC Bayern München": ["bayern", "münchen", "fcb"],
    "Bayer 04 Leverkusen": ["leverkusen", "bayer", "werkself", "b04"],
    "Borussia Dortmund": ["dortmund", "bvb", "borussia dortmund"],
    "RB Leipzig": ["leipzig", "rbl", "rasenballsport"],
    "VfB Stuttgart": ["stuttgart", "vfb"],
    "Eintracht Frankfurt": ["frankfurt", "eintracht", "sge"],
    "SC Freiburg": ["freiburg", "scf", "sport-club"],
    "TSG 1899 Hoffenheim": ["hoffenheim", "tsg"],
    "1. FSV Mainz 05": ["mainz", "fsv mainz", "m05"],
    "SV Werder Bremen": ["bremen", "werder", "svw"],
    "Borussia Mönchengladbach": ["mönchengladbach", "gladbach", "bmg"],
    "1. FC Union Berlin": ["union berlin", "union", "fcu"],
    "FC Augsburg": ["augsburg", "fca"],
    "Hamburger SV": ["hamburger sv", "hamburg", "hsv"],
    "1. FC Köln": ["köln", "effzeh", "1. fc köln"],
    "FC Schalke 04": ["schalke", "s04", "königsblau"],
    "SC Paderborn 07": ["paderborn", "scp"],
    "SV Elversberg": ["elversberg", "sve", "kaiserlinde"],
}


def match_team_name(raw_name: str) -> str:
    """Gleicht Vereinsnamen präzise ab."""
    cleaned = raw_name.lower().strip()

    # 1. Direkter exakter Match
    for full_name in TEAM_POWER_INDEX.keys():
        if cleaned == full_name.lower():
            return full_name

    # 2. Match über eindeutige Aliase
    for full_name, aliases in TEAM_ALIASES.items():
        for alias in aliases:
            if alias in cleaned:
                return full_name

    return raw_name


def fetch_official_matchday_fixtures():
    """Holt die offiziellen Paarungen des anstehenden Spieltags von OpenLigaDB."""
    url = "https://api.openligadb.de/getmatchdata/bl1"
    fixtures = []
    current_matchday_name = "Aktueller Spieltag"

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                current_matchday_name = data[0].get("group", {}).get("groupName", "Aktueller Spieltag")
                for match in data:
                    t1 = match.get("team1", {}).get("teamName", "")
                    t2 = match.get("team2", {}).get("teamName", "")
                    if t1 and t2:
                        fixtures.append((t1, t2))
    except Exception:
        pass

    return fixtures, current_matchday_name


def fetch_season_past_results():
    """
    Analysiert alle bisherigen Spiele der laufenden Saison.
    Liefert Form-Scores und Torstatistiken (wirksam ab Spieltag 2).
    """
    team_stats = {
        team: {"matches": 0, "points": 0, "goals_scored": 0, "goals_conceded": 0, "form_score": 0.0}
        for team in TEAM_POWER_INDEX.keys()
    }

    try:
        url_all_matches = "https://api.openligadb.de/getmatchdata/bl1"
        resp = requests.get(url_all_matches, timeout=10)
        if resp.status_code == 200:
            matches = resp.json()
            finished_count = 0

            for match in matches:
                if not match.get("matchIsFinished", False):
                    continue

                t1_raw = match.get("team1", {}).get("teamName", "")
                t2_raw = match.get("team2", {}).get("teamName", "")
                t1 = match_team_name(t1_raw)
                t2 = match_team_name(t2_raw)

                results = match.get("matchResults", [])
                final_res = next((r for r in results if r.get("resultName") == "Endergebnis"), None)
                if not final_res and results:
                    final_res = results[-1]

                if final_res and t1 in team_stats and t2 in team_stats:
                    finished_count += 1
                    g1 = int(final_res.get("pointsTeam1", 0))
                    g2 = int(final_res.get("pointsTeam2", 0))

                    team_stats[t1]["matches"] += 1
                    team_stats[t2]["matches"] += 1
                    team_stats[t1]["goals_scored"] += g1
                    team_stats[t1]["goals_conceded"] += g2
                    team_stats[t2]["goals_scored"] += g2
                    team_stats[t2]["goals_conceded"] += g1

                    if g1 > g2:
                        team_stats[t1]["points"] += 3
                    elif g1 == g2:
                        team_stats[t1]["points"] += 1
                        team_stats[t2]["points"] += 1
                    else:
                        team_stats[t2]["points"] += 3

            if finished_count > 0:
                for t, s in team_stats.items():
                    if s["matches"] > 0:
                        pts_per_game = s["points"] / s["matches"]
                        goal_diff_per_game = (s["goals_scored"] - s["goals_conceded"]) / s["matches"]
                        s["form_score"] = round((pts_per_game - 1.3) * 1.5 + (goal_diff_per_game * 0.8), 2)
    except Exception:
        pass

    return team_stats


def scrape_ligainsider_live_data():
    """Scraped aktuelle Verletzungen und Ausfälle von LigaInsider."""
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )

    injured_by_team = {team: [] for team in TEAM_POWER_INDEX.keys()}
    url = "https://www.ligainsider.de/verletzungen-bundesliga/"

    try:
        resp = scraper.get(url, timeout=12)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.find_all("tr")
            for row in rows:
                text = row.get_text().lower()
                for full_name, aliases in TEAM_ALIASES.items():
                    if any(alias in text for alias in aliases):
                        player_link = row.find("a")
                        if player_link:
                            p_name = player_link.get_text().strip()
                            if p_name and p_name not in injured_by_team[full_name]:
                                injured_by_team[full_name].append(p_name)
    except Exception:
        pass

    return injured_by_team


def calculate_match_tip(home_team: str, away_team: str, injuries: dict, season_stats: dict) -> dict:
    """Berechnet Tore & Tendenzen basierend auf Kaderstärke, Form und Ausfällen."""
    h_data = TEAM_POWER_INDEX.get(home_team, {"offense": 75, "defense": 75, "overall": 75})
    a_data = TEAM_POWER_INDEX.get(away_team, {"offense": 75, "defense": 75, "overall": 75})

    h_injuries = len(injuries.get(home_team, []))
    a_injuries = len(injuries.get(away_team, []))

    h_form = season_stats.get(home_team, {}).get("form_score", 0.0)
    a_form = season_stats.get(away_team, {}).get("form_score", 0.0)

    # Stärke-Berechnung (+3.0 Heimvorteil)
    home_strength = h_data["overall"] + 3.0 - (h_injuries * 1.2) + h_form
    away_strength = a_data["overall"] - (a_injuries * 1.2) + a_form

    diff = home_strength - away_strength

    h_offense_bonus = (
        (season_stats.get(home_team, {}).get("goals_scored", 0) / max(1, season_stats.get(home_team, {}).get("matches", 1))) * 0.15
        if season_stats.get(home_team, {}).get("matches", 0) > 0 else 0
    )
    a_offense_bonus = (
        (season_stats.get(away_team, {}).get("goals_scored", 0) / max(1, season_stats.get(away_team, {}).get("matches", 1))) * 0.15
        if season_stats.get(away_team, {}).get("matches", 0) > 0 else 0
    )

    home_exp = max(0.5, (h_data["offense"] / 34.0) + (diff * 0.06) - (h_injuries * 0.1) + h_offense_bonus)
    away_exp = max(0.3, (a_data["offense"] / 38.0) - (diff * 0.05) - (a_injuries * 0.1) + a_offense_bonus)

    tip_home = int(np.round(home_exp))
    tip_away = int(np.round(away_exp))

    if abs(diff) < 2.0 and abs(tip_home - tip_away) > 1:
        tip_away = tip_home

    notes = []
    if season_stats.get(home_team, {}).get("matches", 0) > 0:
        notes.append(f"Form: {home_team.split()[-1]} ({'+' if h_form >= 0 else ''}{h_form:.1f}) vs. {away_team.split()[-1]} ({'+' if a_form >= 0 else ''}{a_form:.1f})")
    if h_injuries > 0 or a_injuries > 0:
        notes.append(f"Ausfälle: {home_team.split()[-1]} ({h_injuries}), {away_team.split()[-1]} ({a_injuries})")

    detail_str = f"Stärke: {home_strength:.1f} vs. {away_strength:.1f}"
    if notes:
        detail_str += " | " + " | ".join(notes)

    return {
        "home": home_team,
        "away": away_team,
        "tip": f"{tip_home}:{tip_away}",
        "analysis": detail_str,
    }


def calculate_season_bonus_questions(injuries: dict, season_stats: dict) -> list:
    """Berechnet die 5 Saison-Bonusfragen."""
    teams_sorted_strength = sorted(
        TEAM_POWER_INDEX.items(),
        key=lambda x: x[1]["overall"]
        - (len(injuries.get(x[0], [])) * 0.8)
        + season_stats.get(x[0], {}).get("form_score", 0.0),
        reverse=True,
    )

    teams_sorted_offense = sorted(
        TEAM_POWER_INDEX.items(),
        key=lambda x: x[1]["offense"] + (season_stats.get(x[0], {}).get("goals_scored", 0) * 2),
        reverse=True,
    )

    teams_sorted_pressure = sorted(
        TEAM_POWER_INDEX.items(),
        key=lambda x: x[1]["coach_pressure"]
        + (len(injuries.get(x[0], [])) * 5)
        - (season_stats.get(x[0], {}).get("points", 0) * 4),
        reverse=True,
    )

    champion = teams_sorted_strength[0][0]
    autumn_champ = teams_sorted_strength[0][0]
    top_scorer_team = f"{teams_sorted_offense[0][0]} (Harry Kane)" if "Bayern" in teams_sorted_offense[0][0] else teams_sorted_offense[0][0]
    relegation_teams = [t[0] for t in teams_sorted_strength[-3:]]
    trainer_risk = [t[0] for t in teams_sorted_pressure[:2]]

    return [
        {
            "question": "🏆 Wer wird Deutscher Meister?",
            "answer": champion,
            "confidence": "85%",
            "reasoning": f"Höchster berechneter Gesamtwert ({TEAM_POWER_INDEX[champion]['overall']}) unter Berücksichtigung von Kadertiefe und Performance.",
        },
        {
            "question": "🍂 Wer wird Herbstmeister?",
            "answer": autumn_champ,
            "confidence": "78%",
            "reasoning": "Höchstes Punkterwartungspotenzial bis zur Winterpause.",
        },
        {
            "question": "⚽ Welche Mannschaft stellt den Spieler mit den meisten Toren?",
            "answer": top_scorer_team,
            "confidence": "80%",
            "reasoning": f"Stärkste Offensivwerte ({teams_sorted_offense[0][1]['offense']}) und höchste Großchancen-Generierung.",
        },
        {
            "question": "📉 Welche Mannschaften belegen die Plätze 16-18?",
            "answer": ", ".join(relegation_teams),
            "confidence": "65%",
            "reasoning": "Geringster Stärke-Index und größte Anfälligkeit im Tabellenkeller.",
        },
        {
            "question": "⏱️ Wo findet der erste Trainerwechsel statt?",
            "answer": f"{trainer_risk[0]} (Alternative: {trainer_risk[1]})",
            "confidence": "55%",
            "reasoning": "Höchste Diskrepanz zwischen Erwartungshaltung, Kaderausfällen und Punktestand.",
        },
    ]


def run_kicktipp_live_pipeline():
    """Hauptfunktion: Lädt Spielplan, Saisondaten, LigaInsider-Ausfälle und berechnet die Tipps."""
    fixtures, matchday_label = fetch_official_matchday_fixtures()
    season_stats = fetch_season_past_results()
    injuries = scrape_ligainsider_live_data()

    match_predictions = []
    for h, a in fixtures:
        home_clean = match_team_name(h)
        away_clean = match_team_name(a)
        match_predictions.append(calculate_match_tip(home_clean, away_clean, injuries, season_stats))

    bonus_answers = calculate_season_bonus_questions(injuries, season_stats)
    total_matches_played = sum(s.get("matches", 0) for s in season_stats.values()) // 2

    return {
        "matchday_label": matchday_label,
        "predictions": match_predictions,
        "bonus_questions": bonus_answers,
        "total_injuries_scraped": sum(len(v) for v in injuries.values()),
        "matches_analyzed": total_matches_played,
    }