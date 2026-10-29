"""
test_kicktipp.py – Verifikation der erweiterten KickTipp-Prognose-Engine:
1. Poisson-Modell & Ergebnis-Variation
2. Bisherige Saisonergebnisse, aktuelle Tabellensituation & Tore/Gegentore
3. Schlüsselspieler-Erkennung & stärkere Gewichtung gegenüber Kaderspielern
4. Taktische LigaInsider-Daten (Standards-Stärke vs. Standards-Anfälligkeit, Großchancen, Fehler)
5. Live-End-to-End-Pipeline (OpenLigaDB + LigaInsider)
"""
from kicktipp_engine import (
    calculate_match_tip,
    _poisson_most_likely_score,
    TEAM_POWER_INDEX,
    run_kicktipp_live_pipeline,
)

# Test 1: Poisson-Funktion direkt testen
print("=== TEST 1: Poisson Most-Likely-Score ===")
test_cases = [
    (0.6, 0.5, "Defensiv-Duell"),
    (1.0, 0.8, "Unterdurchschnittlich"),
    (1.3, 1.1, "Durchschnitt"),
    (1.8, 1.2, "Heim leicht überlegen"),
    (2.5, 1.0, "Heim stark überlegen"),
    (2.8, 2.2, "Offensiv-Spektakel"),
    (0.8, 2.0, "Auswärts dominiert"),
    (1.5, 1.5, "Ausgeglichen"),
    (3.0, 0.5, "Totale Heimdominanz"),
]

results = []
for lam_h, lam_a, label in test_cases:
    h, a = _poisson_most_likely_score(lam_h, lam_a)
    result_str = f"{h}:{a}"
    results.append(result_str)
    print(f"  lambda({lam_h:.1f}/{lam_a:.1f}) -> {result_str:>5}  ({label})")

unique_results = set(results)
print(f"\n  Verschiedene Ergebnisse: {len(unique_results)} von {len(results)}")
assert len(unique_results) >= 4, f"FEHLER: Nur {len(unique_results)} verschiedene Ergebnisse!"
print("  [OK] Ausreichende Variation!\n")

# Test 2: Kein Tor-Wert > 6
print("=== TEST 2: Realismus-Check (keine Tore > 6) ===")
for lam_h, lam_a, label in test_cases:
    h, a = _poisson_most_likely_score(lam_h, lam_a)
    assert h <= 6, f"FEHLER: {h} Heimtore bei lambda={lam_h}"
    assert a <= 6, f"FEHLER: {a} Auswärttore bei lambda={lam_a}"
print("  [OK] Alle Ergebnisse realistisch!\n")

# Test 3: Tabellensituation & Tore/Gegentore
print("=== TEST 3: Tabellensituation & Tore/Gegentore ===")
empty_injuries = {t: [] for t in TEAM_POWER_INDEX}
base_stats = {
    t: {
        "rank": 10,
        "matches": 7,
        "points": 10,
        "goals_scored": 10,
        "goals_conceded": 10,
        "form_score": 0.0,
    }
    for t in TEAM_POWER_INDEX
}

res_neutral = calculate_match_tip("SC Freiburg", "SV Werder Bremen", empty_injuries, base_stats)

table_stats = {k: dict(v) for k, v in base_stats.items()}
# Freiburg auf Platz 2 mit starker Offensive (18:6 Tore), Bremen auf Platz 17 mit schwacher Defensive (6:21 Tore)
table_stats["SC Freiburg"] = {
    "rank": 2, "matches": 7, "points": 17, "goals_scored": 18, "goals_conceded": 6,
    "home_matches": 4, "home_goals_scored": 11, "home_goals_conceded": 2, "form_score": 1.8,
}
table_stats["SV Werder Bremen"] = {
    "rank": 17, "matches": 7, "points": 4, "goals_scored": 6, "goals_conceded": 21,
    "away_matches": 4, "away_goals_scored": 2, "away_goals_conceded": 13, "form_score": -1.5,
}
res_table = calculate_match_tip("SC Freiburg", "SV Werder Bremen", empty_injuries, table_stats)
print(
    f"  Neutral: xG {res_neutral['details']['lambda_home']:.2f}:{res_neutral['details']['lambda_away']:.2f} ({res_neutral['tip']}) | "
    f"Mit Tabelle/Toren (#2 18:6 vs #17 6:21): xG {res_table['details']['lambda_home']:.2f}:{res_table['details']['lambda_away']:.2f} ({res_table['tip']})"
)
assert res_table["details"]["lambda_home"] > res_neutral["details"]["lambda_home"] + 0.5, "Tabellen- und Torvorteil muss Heim-xG deutlich steigern!"
assert res_table["details"]["lambda_away"] < res_neutral["details"]["lambda_away"], "Abstiegsplatz & Torflaute muss Auswärts-xG senken!"
print("  [OK] Tabellensituation und Tore/Gegentore wirken korrekt!\n")

# Test 4: Schlüsselspieler-Prüfung (Key Player vs. Kaderspieler)
print("=== TEST 4: Schlüsselspieler-Ausfall vs. ohne Ausfall ===")
inj_key = {t: [] for t in TEAM_POWER_INDEX}
inj_key["FC Bayern München"] = ["Harry Kane", "Jamal Musiala", "Dayot Upamecano"]
res_full_squad = calculate_match_tip("FC Bayern München", "Borussia Dortmund", empty_injuries, base_stats)
res_missing_keys = calculate_match_tip("FC Bayern München", "Borussia Dortmund", inj_key, base_stats)
print(
    f"  Bayern Bestbesetzung: xG {res_full_squad['details']['lambda_home']:.2f}:{res_full_squad['details']['lambda_away']:.2f} ({res_full_squad['tip']})\n"
    f"  Ohne Kane, Musiala & Upamecano: xG {res_missing_keys['details']['lambda_home']:.2f}:{res_missing_keys['details']['lambda_away']:.2f} ({res_missing_keys['tip']})"
)
assert len(res_missing_keys["details"]["home_key_missing"]) == 3, "Alle 3 Bayern-Schlüsselspieler müssen erkannt werden!"
assert res_missing_keys["details"]["lambda_home"] < res_full_squad["details"]["lambda_home"] - 0.3, "Ausfall von Kane & Musiala muss Bayern-xG senken!"
assert res_missing_keys["details"]["lambda_away"] > res_full_squad["details"]["lambda_away"], "Ausfall von Abwehrchef Upamecano muss Gegner-xG erhöhen!"
print("  [OK] Schlüsselspieler-Ausfälle werden erkannt und gewichtet!\n")

# Test 5: Standards-Vorteil (Standards-starkes Team vs. Standards-anfälliges Team)
print("=== TEST 5: Taktischer Standards-Vorteil (LigaInsider-Metriken) ===")
inj_with_tactics = {t: [] for t in TEAM_POWER_INDEX}
inj_with_tactics["_meta"] = {
    "detailed_injuries": {},
    "key_players_missing": {},
    "tactical_by_team": {
        "1. FC Union Berlin": {
            "aerial_won": 120, "aerial_total": 200, "aerial_quote": 60.0,
            "cb_st_aerial_won": 55, "fouls_committed": 35, "big_chances_created": 12,
            "shots_on_target": 25, "errors_before_shot": 1, "gk_crosses_claimed": 8,
            "gk_crosses_total": 10, "gk_box_command_quote": 80.0,
            "ball_recoveries": 95, "turnovers": 30,
            "standard_attack_score": 94.0, "standard_vulnerability_score": 18.0,
        },
        "Borussia Mönchengladbach": {
            "aerial_won": 70, "aerial_total": 160, "aerial_quote": 43.8,
            "cb_st_aerial_won": 18, "fouls_committed": 72, "big_chances_created": 6,
            "shots_on_target": 14, "errors_before_shot": 5, "gk_crosses_claimed": 3,
            "gk_crosses_total": 8, "gk_box_command_quote": 37.5,
            "ball_recoveries": 60, "turnovers": 55,
            "standard_attack_score": 72.0, "standard_vulnerability_score": 44.0,
        },
    },
}
res_no_tac = calculate_match_tip("1. FC Union Berlin", "Borussia Mönchengladbach", empty_injuries, base_stats)
res_tac = calculate_match_tip("1. FC Union Berlin", "Borussia Mönchengladbach", inj_with_tactics, base_stats)
print(
    f"  Union vs. Gladbach Standard-Bonus: +{res_tac['details']['home_standard_bonus']:.2f} xG "
    f"(Gesamt-xG: {res_tac['details']['lambda_home']:.2f} statt {res_no_tac['details']['lambda_home']:.2f})"
)
assert res_tac["details"]["home_standard_bonus"] >= 0.15, "Hoher Standards-Vorteil muss signifikanten Bonus liefern!"
assert any("Standards-Vorteil" in ins for ins in res_tac["details"]["insights"]), "Standards-Insight muss in der Begründung stehen!"
print("  [OK] Standards-Matchup & Taktik-Bonus funktionieren!\n")

# Test 6: Live-Pipeline End-to-End testen
print("=== TEST 6: Live-Pipeline (OpenLigaDB + LigaInsider Live-Scraping) ===")
live_data = run_kicktipp_live_pipeline()
print(f"  Spieltag: {live_data['matchday_label']}")
print(f"  Analysierte Saisonspiele: {live_data['matches_analyzed']}")
print(f"  Gescrapte LigaInsider-Ausfälle: {live_data['total_injuries_scraped']}")
print(f"  Erkannte Schlüsselspieler-Ausfälle: {live_data['total_key_players_missing']}")
for p in live_data["predictions"][:4]:
    print(f"    {p['home']:28s} vs {p['away']:28s} -> {p['tip']} | {p['analysis'][:110]}...")

# Prüfe explizit, dass Jens Stage bei SV Werder Bremen über Vorjahresdaten als fehlender Schlüsselspieler erkannt wird
bremen_match = next((p for p in live_data["predictions"] if p["home"] == "SV Werder Bremen" or p["away"] == "SV Werder Bremen"), None)
assert bremen_match is not None, "SV Werder Bremen muss im Spielplan enthalten sein!"
bremen_keys = (
    bremen_match["details"]["home_key_missing"]
    if bremen_match["home"] == "SV Werder Bremen"
    else bremen_match["details"]["away_key_missing"]
)
stage_entry = next((kp for kp in bremen_keys if "Stage" in kp["name"]), None)
print("  Bremen fehlende Schlüsselspieler:", [f"{kp['name']} ({kp['key_description']})" for kp in bremen_keys])
assert stage_entry is not None, "FEHLER: Jens Stage muss bei Bremen als fehlender Schlüsselspieler aufgelistet sein!"
assert "25/26" in stage_entry["key_description"] or "24/25" in stage_entry["key_description"], (
    "FEHLER: Jens Stage muss Vorjahres-Statistiken von LigaInsider in seiner Beschreibung enthalten!"
)

assert live_data["matches_analyzed"] > 0, "Es müssen echte bisherige Saisonspiele geladen worden sein!"
assert live_data["total_injuries_scraped"] > 0, "Es müssen echte Verletzungen von LigaInsider geladen worden sein!"
assert len(live_data["predictions"]) == 9, "Es müssen 9 Spieltagspaarungen prognostiziert werden!"
print("\n[OK] Alle KickTipp-Tests erfolgreich bestanden!")
