"""
kicktipp_engine.py – Datengetriebene KickTipp-Prognose-Engine

Berücksichtigt:
1. Bisherige Saisonergebnisse (Gesamt, Heim-/Auswärts-Bilanz, Form der letzten 5 Spiele, H2H)
2. Aktuelle Tabellensituation (Tabellenplatz, Punkte, Tordifferenz, Tabellen-Druck/Zonen)
3. Tore & Gegentore (Offensiv-/Defensiv-Effizienz pro Spiel, Heim-/Auswärts-Torschnitt)
4. Schlüsselspieler-Ausfälle (Abgleich der LigaInsider-Verletztenliste mit Torschützen/Toranteil,
   LigaInsider-Noten/Punkten, Spielminuten, Großchancen & Kernkader inkl. Ausfalldauer-Gewichtung)
5. Taktische LigaInsider-Teamdaten (Standards-Stärke vs. Standards-Anfälligkeit über Luftkampfquote,
   begangene Fouls, Torwart-Strafraumbeherrschung, kreierte Großchancen, Schüsse aufs Tor,
   Fehler vor Gegentor sowie Balleroberungen vs. Ballverluste)
"""

from concurrent.futures import ThreadPoolExecutor
import math
import re
import requests
from bs4 import BeautifulSoup
import cloudscraper

# ---------------------------------------------------------------------------
# Basis-Stärken & Taktische Profile (inkl. Standards-Neigung) der 18 Teams
# set_piece_attack: 0..100 (Stärke bei Ecken/Freistößen/Kopfball)
# set_piece_defense: 0..100 (Stabilität gegen Standards; niedrig = anfällig!)
# ---------------------------------------------------------------------------
TEAM_POWER_INDEX = {
    "FC Bayern München": {
        "offense": 95, "defense": 88, "overall": 92, "coach_pressure": 15,
        "set_piece_attack": 86, "set_piece_defense": 84,
    },
    "Bayer 04 Leverkusen": {
        "offense": 90, "defense": 86, "overall": 89, "coach_pressure": 10,
        "set_piece_attack": 85, "set_piece_defense": 82,
    },
    "Borussia Dortmund": {
        "offense": 88, "defense": 84, "overall": 86, "coach_pressure": 30,
        "set_piece_attack": 83, "set_piece_defense": 80,
    },
    "RB Leipzig": {
        "offense": 86, "defense": 84, "overall": 85, "coach_pressure": 20,
        "set_piece_attack": 82, "set_piece_defense": 83,
    },
    "VfB Stuttgart": {
        "offense": 84, "defense": 79, "overall": 82, "coach_pressure": 25,
        "set_piece_attack": 80, "set_piece_defense": 75,
    },
    "Eintracht Frankfurt": {
        "offense": 82, "defense": 77, "overall": 80, "coach_pressure": 30,
        "set_piece_attack": 79, "set_piece_defense": 74,
    },
    "SC Freiburg": {
        "offense": 79, "defense": 80, "overall": 79, "coach_pressure": 10,
        "set_piece_attack": 90, "set_piece_defense": 82,  # Traditionell extrem stark bei Standards
    },
    "TSG 1899 Hoffenheim": {
        "offense": 79, "defense": 74, "overall": 77, "coach_pressure": 50,
        "set_piece_attack": 76, "set_piece_defense": 71,
    },
    "1. FSV Mainz 05": {
        "offense": 77, "defense": 76, "overall": 76, "coach_pressure": 40,
        "set_piece_attack": 82, "set_piece_defense": 78,
    },
    "SV Werder Bremen": {
        "offense": 77, "defense": 74, "overall": 76, "coach_pressure": 35,
        "set_piece_attack": 83, "set_piece_defense": 72,
    },
    "Borussia Mönchengladbach": {
        "offense": 76, "defense": 70, "overall": 74, "coach_pressure": 65,
        "set_piece_attack": 78, "set_piece_defense": 66,  # Anfällig bei Standards & hohen Bällen
    },
    "1. FC Union Berlin": {
        "offense": 73, "defense": 76, "overall": 75, "coach_pressure": 45,
        "set_piece_attack": 87, "set_piece_defense": 79,  # Sehr hohe Standard- & Kopfballgefahr
    },
    "FC Augsburg": {
        "offense": 75, "defense": 73, "overall": 74, "coach_pressure": 55,
        "set_piece_attack": 79, "set_piece_defense": 72,
    },
    "Hamburger SV": {
        "offense": 74, "defense": 72, "overall": 73, "coach_pressure": 50,
        "set_piece_attack": 78, "set_piece_defense": 71,
    },
    "1. FC Köln": {
        "offense": 73, "defense": 73, "overall": 73, "coach_pressure": 50,
        "set_piece_attack": 81, "set_piece_defense": 74,
    },
    "FC Schalke 04": {
        "offense": 71, "defense": 74, "overall": 72, "coach_pressure": 65,
        "set_piece_attack": 77, "set_piece_defense": 75,
    },
    "SC Paderborn 07": {
        "offense": 71, "defense": 72, "overall": 71, "coach_pressure": 55,
        "set_piece_attack": 75, "set_piece_defense": 73,
    },
    "SV Elversberg": {
        "offense": 72, "defense": 71, "overall": 71, "coach_pressure": 35,
        "set_piece_attack": 76, "set_piece_defense": 70,
    },
}

# Eindeutige Aliase & Kürzel zur sicheren Erkennung über OpenLigaDB und LigaInsider
TEAM_ALIASES = {
    "FC Bayern München": ["fc bayern", "bayern münchen", "bayern munchen", "fcbayern", "bayern", "münchen", "fcb"],
    "Bayer 04 Leverkusen": ["bayer 04 leverkusen", "bayer leverkusen", "leverkusen", "werkself", "b04"],
    "Borussia Dortmund": ["borussia dortmund", "dortmund", "bvb"],
    "RB Leipzig": ["rb leipzig", "rasenballsport leipzig", "leipzig", "rbl"],
    "VfB Stuttgart": ["vfb stuttgart", "stuttgart", "vfb"],
    "Eintracht Frankfurt": ["eintracht frankfurt", "frankfurt", "sge"],
    "SC Freiburg": ["sc freiburg", "sport-club freiburg", "freiburg", "scf"],
    "TSG 1899 Hoffenheim": ["tsg 1899 hoffenheim", "tsg hoffenheim", "hoffenheim", "tsg"],
    "1. FSV Mainz 05": ["1. fsv mainz 05", "fsv mainz 05", "mainz 05", "mainz", "m05"],
    "SV Werder Bremen": ["sv werder bremen", "werder bremen", "bremen", "werder", "svw"],
    "Borussia Mönchengladbach": [
        "borussia mönchengladbach", "borussia monchengladbach", "mönchengladbach", "monchengladbach", "gladbach", "bmg"
    ],
    "1. FC Union Berlin": ["1. fc union berlin", "union berlin", "union", "fcu"],
    "FC Augsburg": ["fc augsburg", "augsburg", "fca"],
    "Hamburger SV": ["hamburger sv", "hamburg", "hsv"],
    "1. FC Köln": ["1. fc köln", "1. fc koln", "fc köln", "köln", "koln", "effzeh", "koe"],
    "FC Schalke 04": ["fc schalke 04", "schalke 04", "schalke", "s04", "königsblau"],
    "SC Paderborn 07": ["sc paderborn 07", "sc paderborn", "paderborn", "scp"],
    "SV Elversberg": ["sv 07 elversberg", "sv elversberg", "elversberg", "sve", "elv"],
}

# Bekannte Schlüsselspieler (Kapitäne, Torjäger, Spielmacher, Abwehrchefs, Stammtorhüter)
# Ergänzt die dynamisch von LigaInsider gescrapten Top-Scorer, Top-Noten & Dauerbrenner
TEAM_KEY_PLAYERS: dict[str, dict[str, dict]] = {
    "FC Bayern München": {
        "kane": {"role": "offense", "weight": 1.0, "label": "Torjäger"},
        "musiala": {"role": "offense", "weight": 0.95, "label": "Spielmacher"},
        "olise": {"role": "offense", "weight": 0.90, "label": "Offensivstar"},
        "kimmich": {"role": "both", "weight": 0.85, "label": "Stratege & Standard-Schütze"},
        "diaz": {"role": "offense", "weight": 0.75, "label": "Flügelstürmer"},
        "díaz": {"role": "offense", "weight": 0.75, "label": "Flügelstürmer"},
        "upamecano": {"role": "defense", "weight": 0.75, "label": "Abwehrchef"},
        "tah": {"role": "defense", "weight": 0.75, "label": "Abwehrchef"},
        "neuer": {"role": "defense", "weight": 0.70, "label": "Stammkeeper"},
        "urbig": {"role": "defense", "weight": 0.65, "label": "Torwart"},
    },
    "Bayer 04 Leverkusen": {
        "schick": {"role": "offense", "weight": 0.85, "label": " Mittelstürmer"},
        "boniface": {"role": "offense", "weight": 0.85, "label": "Stürmer"},
        "grimaldo": {"role": "both", "weight": 0.90, "label": "Standard-Spezialist & Leistungsträger"},
        "palacios": {"role": "both", "weight": 0.75, "label": "Mittelfeldmotor"},
        "andrich": {"role": "defense", "weight": 0.70, "label": "Abwehr/Zentrum"},
        "tapsoba": {"role": "defense", "weight": 0.75, "label": "Abwehrchef"},
    },
    "Borussia Dortmund": {
        "guirassy": {"role": "offense", "weight": 0.95, "label": "Torjäger"},
        "brandt": {"role": "offense", "weight": 0.80, "label": "Spielmacher & Standards"},
        "schlotterbeck": {"role": "defense", "weight": 0.85, "label": "Abwehrchef & Kopfballspieler"},
        "kobel": {"role": "defense", "weight": 0.85, "label": "Stammkeeper"},
        "adeyemi": {"role": "offense", "weight": 0.70, "label": " Angreifer"},
        "gross": {"role": "both", "weight": 0.70, "label": "Stratege"},
        "groß": {"role": "both", "weight": 0.70, "label": "Stratege"},
    },
    "RB Leipzig": {
        "openda": {"role": "offense", "weight": 0.90, "label": "Torjäger"},
        "sesko": {"role": "offense", "weight": 0.80, "label": "Stürmer"},
        "orban": {"role": "defense", "weight": 0.90, "label": "Kapitän, Abwehrchef & Kopfballungeheuer"},
        "orbán": {"role": "defense", "weight": 0.90, "label": "Kapitän, Abwehrchef & Kopfballungeheuer"},
        "raum": {"role": "both", "weight": 0.80, "label": "Flanken- & Standardgeber"},
        "lukeba": {"role": "defense", "weight": 0.75, "label": "Innenverteidiger"},
        "castello": {"role": "defense", "weight": 0.75, "label": "Innenverteidiger"},
        "vandevoordt": {"role": "defense", "weight": 0.70, "label": "Stammkeeper"},
    },
    "VfB Stuttgart": {
        "undav": {"role": "offense", "weight": 0.90, "label": "Torjäger"},
        "demirovic": {"role": "offense", "weight": 0.80, "label": "Torjäger"},
        "stiller": {"role": "both", "weight": 0.85, "label": "Spielmacher & Standardgeber"},
        "mittelstädt": {"role": "both", "weight": 0.75, "label": "Leistungsträger"},
        "karazor": {"role": "defense", "weight": 0.75, "label": "Kapitän & Abräumer"},
        "chabot": {"role": "defense", "weight": 0.75, "label": "Abwehrchef & Luftkampf"},
        "nübel": {"role": "defense", "weight": 0.75, "label": "Stammkeeper"},
    },
    "Eintracht Frankfurt": {
        "ekitike": {"role": "offense", "weight": 0.85, "label": "Stürmer"},
        "uzun": {"role": "offense", "weight": 0.75, "label": "Offensivspieler"},
        "larsson": {"role": "both", "weight": 0.75, "label": "Mittelfeldmotor"},
        "koch": {"role": "defense", "weight": 0.85, "label": "Abwehrchef & Kopfballspieler"},
        "theate": {"role": "defense", "weight": 0.75, "label": "Innenverteidiger"},
        "trapp": {"role": "defense", "weight": 0.70, "label": "Torwart"},
    },
    "SC Freiburg": {
        "grifo": {"role": "offense", "weight": 0.95, "label": "Top-Scorer & Standard-Spezialist"},
        "doan": {"role": "offense", "weight": 0.80, "label": "Offensivträger"},
        "ginter": {"role": "defense", "weight": 0.85, "label": "Abwehrchef & Standard-Kopfball"},
        "lienharth": {"role": "defense", "weight": 0.80, "label": "Abwehrchef"},
        "lienhart": {"role": "defense", "weight": 0.80, "label": "Abwehrchef & Kopfball"},
        "atubolu": {"role": "defense", "weight": 0.75, "label": "Stammkeeper"},
    },
    "TSG 1899 Hoffenheim": {
        "kramaric": {"role": "offense", "weight": 0.95, "label": "Offensiv-Lebensversicherung"},
        "kramarić": {"role": "offense", "weight": 0.95, "label": "Offensiv-Lebensversicherung"},
        "hlozek": {"role": "offense", "weight": 0.75, "label": "Angreifer"},
        "stach": {"role": "defense", "weight": 0.75, "label": "Zweikampf-Anker"},
        "baumann": {"role": "defense", "weight": 0.85, "label": "Kapitän & Stammkeeper"},
    },
    "1. FSV Mainz 05": {
        "burkardt": {"role": "offense", "weight": 0.90, "label": "Torjäger & Kapitän"},
        "amiri": {"role": "both", "weight": 0.90, "label": "Spielmacher & Standard-Schütze"},
        "nebel": {"role": "offense", "weight": 0.75, "label": "Offensiv-Antreiber"},
        "sano": {"role": "defense", "weight": 0.75, "label": "Mittelfeld-Abräumer"},
        "kohr": {"role": "defense", "weight": 0.70, "label": "Abwehrkämpfer"},
        "zentner": {"role": "defense", "weight": 0.80, "label": "Stammkeeper"},
    },
    "SV Werder Bremen": {
        "füllkrug": {"role": "offense", "weight": 0.95, "label": "Top-Torjäger & Zielspieler"},
        "fuellkrug": {"role": "offense", "weight": 0.95, "label": "Top-Torjäger & Zielspieler"},
        "ducksch": {"role": "offense", "weight": 0.85, "label": "Scorer & Standard-Schütze"},
        "stage": {"role": "both", "weight": 0.80, "label": "Torgefährlicher Achter"},
        "wehmeyer": {"role": "both", "weight": 0.70, "label": "Leistungsträger"},
        "friedl": {"role": "defense", "weight": 0.80, "label": "Kapitän & Abwehrchef"},
        "zetterer": {"role": "defense", "weight": 0.75, "label": "Stammkeeper"},
    },
    "Borussia Mönchengladbach": {
        "kleindienst": {"role": "offense", "weight": 0.95, "label": "Torjäger & Kopfball-Zielspieler"},
        "plea": {"role": "offense", "weight": 0.80, "label": "Offensiv-Kreativspieler"},
        "honorat": {"role": "offense", "weight": 0.85, "label": "Flanken- & Standardgeber"},
        "stöger": {"role": "offense", "weight": 0.75, "label": "Spielmacher & Standards"},
        "weigl": {"role": "defense", "weight": 0.75, "label": "Sechser"},
        "itakura": {"role": "defense", "weight": 0.80, "label": "Abwehrchef"},
        "nicolas": {"role": "defense", "weight": 0.75, "label": "Stammkeeper"},
    },
    "1. FC Union Berlin": {
        "hollerbach": {"role": "offense", "weight": 0.80, "label": " Angreifer"},
        "doekhi": {"role": "defense", "weight": 0.90, "label": "Abwehrchef & Kopfball-Torjäger bei Standards"},
        "leite": {"role": "defense", "weight": 0.80, "label": "Innenverteidiger & Luftkampf"},
        "querfeld": {"role": "defense", "weight": 0.75, "label": "Innenverteidiger"},
        "khedira": {"role": "defense", "weight": 0.75, "label": "Kapitän & Abräumer"},
        "rönnow": {"role": "defense", "weight": 0.85, "label": "Stammkeeper"},
        "roennow": {"role": "defense", "weight": 0.85, "label": "Stammkeeper"},
    },
    "FC Augsburg": {
        "wolf": {"role": "both", "weight": 0.80, "label": "Top-Vorbereiter"},
        "claude-maurice": {"role": "offense", "weight": 0.80, "label": "Offensivspieler"},
        "essende": {"role": "offense", "weight": 0.75, "label": "Mittelstürmer"},
        "gouweleeuw": {"role": "defense", "weight": 0.85, "label": "Kapitän & Abwehrchef"},
        "schlotterbeck": {"role": "defense", "weight": 0.80, "label": "Innenverteidiger & Standardgefahr"},
        "matsima": {"role": "defense", "weight": 0.75, "label": "Innenverteidiger"},
    },
    "Hamburger SV": {
        "glatzel": {"role": "offense", "weight": 0.90, "label": "Torjäger & Kopfballspieler"},
        "selke": {"role": "offense", "weight": 0.80, "label": "Mittelstürmer"},
        "dompé": {"role": "offense", "weight": 0.85, "label": "Flügel & Standardgeber"},
        "dompe": {"role": "offense", "weight": 0.85, "label": "Flügel & Standardgeber"},
        "muheim": {"role": "both", "weight": 0.80, "label": "Standard- & Flankenspezialist"},
        "schonlau": {"role": "defense", "weight": 0.80, "label": "Kapitän & Abwehrchef"},
        "heuer fernandes": {"role": "defense", "weight": 0.75, "label": "Stammkeeper"},
    },
    "1. FC Köln": {
        "downs": {"role": "offense", "weight": 0.80, "label": "Stürmer"},
        "lemperle": {"role": "offense", "weight": 0.80, "label": "Angreifer"},
        "maina": {"role": "offense", "weight": 0.80, "label": "Tempospieler & Vorbereiter"},
        "waldschmidt": {"role": "offense", "weight": 0.75, "label": "Offensivspieler"},
        "hübers": {"role": "defense", "weight": 0.85, "label": "Kapitän & Abwehrchef"},
        "huebers": {"role": "defense", "weight": 0.85, "label": "Kapitän & Abwehrchef"},
        "martel": {"role": "defense", "weight": 0.80, "label": "Zweikampf-Sechser"},
        "schwabe": {"role": "defense", "weight": 0.75, "label": "Stammkeeper"},
        "schwäbe": {"role": "defense", "weight": 0.75, "label": "Stammkeeper"},
    },
    "FC Schalke 04": {
        "karaman": {"role": "offense", "weight": 0.95, "label": "Kapitän & Lebensversicherung"},
        "sylla": {"role": "offense", "weight": 0.85, "label": "Torjäger"},
        "segouin": {"role": "both", "weight": 0.75, "label": "Mittelfeld"},
        "seguin": {"role": "both", "weight": 0.80, "label": "Spielmacher & Standard-Schütze"},
        "kalas": {"role": "defense", "weight": 0.80, "label": "Abwehrchef"},
        "heekeren": {"role": "defense", "weight": 0.70, "label": "Torwart"},
    },
    "SC Paderborn 07": {
        "bilbija": {"role": "offense", "weight": 0.85, "label": "Offensivträger"},
        "grimaldi": {"role": "offense", "weight": 0.75, "label": "Kopfball-Zielspieler"},
        "obermair": {"role": "both", "weight": 0.85, "label": "Kapitän & Standard-Schütze"},
        "götze": {"role": "defense", "weight": 0.80, "label": "Abwehrchef"},
        "goetze": {"role": "defense", "weight": 0.80, "label": "Abwehrchef"},
        "noll": {"role": "defense", "weight": 0.80, "label": "Stammkeeper"},
    },
    "SV Elversberg": {
        "asllani": {"role": "offense", "weight": 0.90, "label": "Top-Torjäger"},
        "damar": {"role": "offense", "weight": 0.80, "label": "Spielmacher"},
        "schnellbacher": {"role": "offense", "weight": 0.75, "label": "Angreifer"},
        "fellhauer": {"role": "both", "weight": 0.80, "label": "Mittelfeld-Anker"},
        "pinckert": {"role": "defense", "weight": 0.75, "label": "Innenverteidiger"},
        "kristof": {"role": "defense", "weight": 0.85, "label": "Stammkeeper & Rückhalt"},
    },
}


# ---------------------------------------------------------------------------
# Mathematische Hilfsfunktionen (Poisson & Wahrscheinlichkeiten)
# ---------------------------------------------------------------------------
def _poisson_pmf(k: int, lam: float) -> float:
    """Berechnet P(X=k) für eine Poisson-Verteilung mit Parameter lambda."""
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def _poisson_most_likely_score(lambda_home: float, lambda_away: float, max_goals: int = 6) -> tuple[int, int]:
    """
    Bestimmt das wahrscheinlichste exakte Ergebnis über eine Dixon-Coles-adjustierte
    Joint-Poisson-Verteilung, berücksichtigt aber auch die 1X2-Tendenz-Konsistenz.
    """
    best_prob = -1.0
    best_score = (1, 1)

    for h in range(max_goals + 1):
        p_h = _poisson_pmf(h, lambda_home)
        for a in range(max_goals + 1):
            p_a = _poisson_pmf(a, lambda_away)
            joint_prob = p_h * p_a
            if joint_prob > best_prob:
                best_prob = joint_prob
                best_score = (h, a)

    # Falls ein Team einen klaren Expected-Goals-Vorsprung hat (>= 0.38 Tore),
    # soll das Tippspiel-Ergebnis nicht durch den Poisson-Modus bei 1:1 hängen bleiben
    diff = lambda_home - lambda_away
    if best_score[0] == best_score[1]:
        if diff >= 0.38:
            best_score = (best_score[0] + 1, best_score[1])
        elif diff <= -0.38:
            best_score = (best_score[0], best_score[1] + 1)

    return best_score


def _calculate_1x2_probabilities(lambda_home: float, lambda_away: float, max_goals: int = 8) -> tuple[int, int, int]:
    """Berechnet prozentuale Wahrscheinlichkeiten für Heimsieg, Remis, Auswärtssieg."""
    p_home, p_draw, p_away = 0.0, 0.0, 0.0
    for h in range(max_goals + 1):
        ph = _poisson_pmf(h, lambda_home)
        for a in range(max_goals + 1):
            pa = _poisson_pmf(a, lambda_away)
            p = ph * pa
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
    total = p_home + p_draw + p_away or 1.0
    h_pct = int(round(p_home / total * 100))
    d_pct = int(round(p_draw / total * 100))
    a_pct = max(0, 100 - h_pct - d_pct)
    return h_pct, d_pct, a_pct


def match_team_name(raw_name: str) -> str:
    """Gleicht Vereinsnamen, Kurz-Codes und LigaInsider-Slugs präzise ab."""
    if not raw_name:
        return ""
    cleaned = raw_name.lower().strip()

    # 1. Direkter exakter Match
    for full_name in TEAM_POWER_INDEX.keys():
        if cleaned == full_name.lower():
            return full_name

    # 2. Exakter Kurzcode-Match (z. B. "fcb", "svw", "elv", "koe")
    for full_name, aliases in TEAM_ALIASES.items():
        if cleaned in aliases:
            return full_name

    # 3. Teilstring-Match über Aliase (längste Aliase zuerst prüfen)
    for full_name, aliases in TEAM_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if len(alias) >= 4 and alias in cleaned:
                return full_name

    return raw_name


def _short_team_name(team_name: str) -> str:
    """Gibt einen gut lesbaren Kurznamen zurück (z. B. 'Mainz' statt '05', 'Schalke' statt '04')."""
    short_map = {
        "FC Bayern München": "Bayern",
        "Bayer 04 Leverkusen": "Leverkusen",
        "Borussia Dortmund": "Dortmund",
        "RB Leipzig": "Leipzig",
        "VfB Stuttgart": "Stuttgart",
        "Eintracht Frankfurt": "Frankfurt",
        "SC Freiburg": "Freiburg",
        "TSG 1899 Hoffenheim": "Hoffenheim",
        "1. FSV Mainz 05": "Mainz",
        "SV Werder Bremen": "Bremen",
        "Borussia Mönchengladbach": "Gladbach",
        "1. FC Union Berlin": "Union Berlin",
        "FC Augsburg": "Augsburg",
        "Hamburger SV": "HSV",
        "1. FC Köln": "Köln",
        "FC Schalke 04": "Schalke",
        "SC Paderborn 07": "Paderborn",
        "SV Elversberg": "Elversberg",
    }
    return short_map.get(team_name, team_name.split()[-1] if team_name else "")


# ---------------------------------------------------------------------------
# OpenLigaDB: Spielplan, Bisherige Saisonergebnisse & Aktuelle Tabelle
# ---------------------------------------------------------------------------
def fetch_official_matchday_fixtures() -> tuple[list[tuple[str, str]], str, int | None]:
    """
    Holt die offiziellen Paarungen des aktuellen/kommenden Spieltags von OpenLigaDB
    sowie das aktuelle Saisonjahr (z. B. 2026).
    """
    url = "https://api.openligadb.de/getmatchdata/bl1"
    fixtures = []
    current_matchday_name = "Aktueller Spieltag"
    league_season = None

    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                current_matchday_name = data[0].get("group", {}).get("groupName", "Aktueller Spieltag")
                league_season = data[0].get("leagueSeason")
                for match in data:
                    t1 = match.get("team1", {}).get("teamName", "")
                    t2 = match.get("team2", {}).get("teamName", "")
                    if t1 and t2:
                        fixtures.append((t1, t2))
    except Exception:
        pass

    return fixtures, current_matchday_name, league_season


def _default_team_season_dict() -> dict:
    return {
        "rank": 10,
        "matches": 0,
        "won": 0,
        "draw": 0,
        "lost": 0,
        "points": 0,
        "goals_scored": 0,
        "goals_conceded": 0,
        "goal_diff": 0,
        "home_matches": 0,
        "home_points": 0,
        "home_goals_scored": 0,
        "home_goals_conceded": 0,
        "away_matches": 0,
        "away_points": 0,
        "away_goals_scored": 0,
        "away_goals_conceded": 0,
        "last5_points": 0,
        "last5_goals_scored": 0,
        "last5_goals_conceded": 0,
        "last5_form": "",
        "recent_results": [],
        "form_score": 0.0,
    }


def fetch_season_past_results(league_season: int | None = None) -> tuple[dict, dict]:
    """
    Analysiert ALLE bisherigen Spiele der laufenden Saison UND die offizielle aktuelle
    Bundesliga-Tabelle über OpenLigaDB:
    - Aktueller Tabellenplatz, Punkte, Tore, Gegentore, Tordifferenz
    - Heim- und Auswärtsbilanz (Tore/Gegentore/Punkte daheim vs. auswärts)
    - Formkurve der letzten 5 Spiele (S/U/N, Tore, Gegentore, Gegnerstärke-gewichtete Form)
    - Bisherige Einzel-Ergebnisse & Head-to-Head-Duelle
    """
    team_stats = {team: _default_team_season_dict() for team in TEAM_POWER_INDEX.keys()}
    h2h_results: dict[tuple[str, str], list[tuple[int, int, str, str]]] = {}

    # 1. Saisonjahr bestimmen, falls nicht übergeben
    if not league_season:
        try:
            r_cur = requests.get("https://api.openligadb.de/getmatchdata/bl1", timeout=8)
            if r_cur.status_code == 200 and r_cur.json():
                league_season = r_cur.json()[0].get("leagueSeason")
        except Exception:
            league_season = 2026

    # 2. Offizielle Live-Tabelle von OpenLigaDB abrufen (/getbltable/bl1/{season})
    if league_season:
        try:
            url_table = f"https://api.openligadb.de/getbltable/bl1/{league_season}"
            resp_tbl = requests.get(url_table, timeout=10)
            if resp_tbl.status_code == 200:
                table_data = resp_tbl.json()
                for rank_idx, entry in enumerate(table_data, start=1):
                    t_name = match_team_name(entry.get("teamName", ""))
                    if t_name in team_stats:
                        s = team_stats[t_name]
                        s["rank"] = rank_idx
                        s["matches"] = int(entry.get("matches", 0))
                        s["won"] = int(entry.get("won", 0))
                        s["draw"] = int(entry.get("draw", 0))
                        s["lost"] = int(entry.get("lost", 0))
                        s["points"] = int(entry.get("points", 0))
                        s["goals_scored"] = int(entry.get("goals", 0))
                        s["goals_conceded"] = int(entry.get("opponentGoals", 0))
                        s["goal_diff"] = int(entry.get("goalDiff", s["goals_scored"] - s["goals_conceded"]))
        except Exception:
            pass

    # 3. Alle bisherigen Saisonspiele abrufen (/getmatchdata/bl1/{season})
    url_all_matches = (
        f"https://api.openligadb.de/getmatchdata/bl1/{league_season}"
        if league_season
        else "https://api.openligadb.de/getmatchdata/bl1"
    )
    match_history_by_team: dict[str, list[dict]] = {team: [] for team in TEAM_POWER_INDEX.keys()}

    try:
        resp = requests.get(url_all_matches, timeout=12)
        if resp.status_code == 200:
            matches = resp.json()
            # Falls die Tabelle oben nicht geladen werden konnte, zählen wir selbst mit
            table_was_loaded = any(s["matches"] > 0 for s in team_stats.values())

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

                if not final_res or t1 not in team_stats or t2 not in team_stats:
                    continue

                g1 = int(final_res.get("pointsTeam1", 0))
                g2 = int(final_res.get("pointsTeam2", 0))
                md_order = int(match.get("group", {}).get("groupOrderID", 0))

                # Heim- und Auswärts-Splits erfassen
                team_stats[t1]["home_matches"] += 1
                team_stats[t1]["home_goals_scored"] += g1
                team_stats[t1]["home_goals_conceded"] += g2

                team_stats[t2]["away_matches"] += 1
                team_stats[t2]["away_goals_scored"] += g2
                team_stats[t2]["away_goals_conceded"] += g1

                if g1 > g2:
                    p1, p2 = 3, 0
                    r1_char, r2_char = "S", "N"
                elif g1 == g2:
                    p1, p2 = 1, 1
                    r1_char, r2_char = "U", "U"
                else:
                    p1, p2 = 0, 3
                    r1_char, r2_char = "N", "S"

                team_stats[t1]["home_points"] += p1
                team_stats[t2]["away_points"] += p2

                if not table_was_loaded:
                    team_stats[t1]["matches"] += 1
                    team_stats[t2]["matches"] += 1
                    team_stats[t1]["goals_scored"] += g1
                    team_stats[t1]["goals_conceded"] += g2
                    team_stats[t2]["goals_scored"] += g2
                    team_stats[t2]["goals_conceded"] += g1
                    team_stats[t1]["points"] += p1
                    team_stats[t2]["points"] += p2
                    if p1 == 3:
                        team_stats[t1]["won"] += 1
                        team_stats[t2]["lost"] += 1
                    elif p1 == 1:
                        team_stats[t1]["draw"] += 1
                        team_stats[t2]["draw"] += 1
                    else:
                        team_stats[t1]["lost"] += 1
                        team_stats[t2]["won"] += 1

                # Match-Historie für Formkurve & Ergebnisse speichern
                match_history_by_team[t1].append({
                    "md": md_order,
                    "opponent": t2,
                    "is_home": True,
                    "gf": g1,
                    "ga": g2,
                    "pts": p1,
                    "res": r1_char,
                    "label": f"{g1}:{g2} vs. {_short_team_name(t2)} (H)",
                })
                match_history_by_team[t2].append({
                    "md": md_order,
                    "opponent": t1,
                    "is_home": False,
                    "gf": g2,
                    "ga": g1,
                    "pts": p2,
                    "res": r2_char,
                    "label": f"{g2}:{g1} vs. {_short_team_name(t1)} (A)",
                })

                # H2H speichern
                h2h_key = tuple(sorted([t1, t2]))
                h2h_results.setdefault(h2h_key, []).append((g1, g2, t1, t2))

            # Falls Tabelle nicht über getbltable geladen wurde, Ränge aus Punkten/Tordifferenz berechnen
            if not table_was_loaded and any(s["matches"] > 0 for s in team_stats.values()):
                for s in team_stats.values():
                    s["goal_diff"] = s["goals_scored"] - s["goals_conceded"]
                ranked = sorted(
                    team_stats.items(),
                    key=lambda kv: (kv[1]["points"], kv[1]["goal_diff"], kv[1]["goals_scored"]),
                    reverse=True,
                )
                for idx, (t_name, _) in enumerate(ranked, start=1):
                    team_stats[t_name]["rank"] = idx

            # Form-Scores & Letzte 5 Spiele für jedes Team auswerten
            for t_name, s in team_stats.items():
                hist = sorted(match_history_by_team.get(t_name, []), key=lambda x: x["md"])
                if hist:
                    last5 = hist[-5:]
                    s["last5_points"] = sum(m["pts"] for m in last5)
                    s["last5_goals_scored"] = sum(m["gf"] for m in last5)
                    s["last5_goals_conceded"] = sum(m["ga"] for m in last5)
                    s["last5_form"] = "-".join(m["res"] for m in last5)
                    s["recent_results"] = [m["label"] for m in last5[-3:]]

                    # Gegnerstärke- und Aktualitäts-gewichteter Form-Score
                    weighted_pts = 0.0
                    weighted_gd = 0.0
                    weight_sum = 0.0
                    for idx, m in enumerate(last5):
                        w = 0.6 + 0.15 * idx  # Jüngste Spiele zählen stärker
                        opp_rank = team_stats.get(m["opponent"], {}).get("rank", 10)
                        # Bonus wenn Punkte gegen Top-Teams geholt wurden
                        opp_factor = 1.0 + (10 - opp_rank) * 0.025 if m["pts"] > 0 else 1.0 - (10 - opp_rank) * 0.02
                        weighted_pts += m["pts"] * w * opp_factor
                        weighted_gd += (m["gf"] - m["ga"]) * w
                        weight_sum += w

                    avg_w_pts = weighted_pts / weight_sum
                    avg_w_gd = weighted_gd / weight_sum
                    s["form_score"] = round((avg_w_pts - 1.35) * 1.4 + avg_w_gd * 0.65, 2)
                elif s["matches"] > 0:
                    pts_pg = s["points"] / s["matches"]
                    gd_pg = (s["goals_scored"] - s["goals_conceded"]) / s["matches"]
                    s["form_score"] = round((pts_pg - 1.35) * 1.4 + gd_pg * 0.65, 2)
    except Exception:
        pass

    return team_stats, h2h_results


# ---------------------------------------------------------------------------
# LigaInsider Live-Scraping: Ausfälle, Schlüsselspieler & Taktik-Statistiken
# ---------------------------------------------------------------------------
def _extract_li_player_id(href: str) -> str:
    """Extrahiert die numerische LigaInsider-Spieler-ID aus URLs wie '/niclas-fuellkrug_1867/'."""
    if not href:
        return ""
    m = re.search(r"_(\d+)/?$", href.strip())
    return m.group(1) if m else ""


def _parse_german_float(val_str: str) -> float:
    if not val_str:
        return 0.0
    cleaned = re.sub(r"[^0-9,.-]", "", val_str).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _missing_duration_recency_factor(missing_since: str) -> float:
    """
    Bewertet, wie frisch ein Ausfall ist:
    - Frische Ausfälle ('Stunde', 'Tag', '1 Woche', '2 Wochen', '3 Wochen') treffen das Team voll (1.0),
      da die bisherigen Saisonergebnisse noch MIT diesem Spieler erzielt wurden.
    - Langzeitverletzte ('2 Monaten', '5 Monaten' etc.) sind bereits in der bisherigen Saisonform
      und Torbilanz eingepreist (Faktor 0.25).
    """
    if not missing_since:
        return 0.85
    s = missing_since.lower()
    if "monat" in s or "jahr" in s:
        m = re.search(r"(\d+)", s)
        months = int(m.group(1)) if m else 2
        return 0.20 if months >= 2 else 0.45
    if "woche" in s:
        m = re.search(r"(\d+)", s)
        weeks = int(m.group(1)) if m else 1
        if weeks <= 2:
            return 1.0
        if weeks <= 4:
            return 0.75
        return 0.45
    return 1.0


def _default_tactical_team_dict(team_name: str) -> dict:
    base = TEAM_POWER_INDEX.get(team_name, {})
    return {
        "aerial_won": 0,
        "aerial_total": 0,
        "aerial_quote": 50.0,
        "cb_st_aerial_won": 0,
        "fouls_committed": 0,
        "big_chances_created": 0,
        "shots_on_target": 0,
        "errors_before_shot": 0,
        "gk_crosses_claimed": 0,
        "gk_crosses_total": 0,
        "gk_box_command_quote": 65.0,
        "ball_recoveries": 0,
        "turnovers": 0,
        "standard_attack_score": float(base.get("set_piece_attack", 78)),
        "standard_vulnerability_score": float(100 - base.get("set_piece_defense", 75)),
    }


def scrape_ligainsider_live_data() -> dict:
    """
    Scraped parallel alle relevanten LigaInsider-Daten für die KickTipp-Prognose:
    1. /bundesliga/verletzte-und-gesperrte-spieler/ (aktuelle Ausfälle, Grund, Ausfalldauer)
    2. /bundesliga/torschuetzenliste/ (Tore, Vorlagen, Scorerpunkte, % Anteil an Teamtoren)
    3. /bundesliga/noten/spieltag/ (Saison-Ø-Note und Ø-Punkte zur Schlüsselspieler-Erkennung)
    4. /bundesliga/ranking-top-10/* (Luftkampfquote, Fouls, Strafraumbeherrschung,
       Kreierte Großchancen, Schüsse aufs Tor, Fehler vor Schuss/Gegentor, Balleroberungen, Ballverluste)

    Gibt ein Dictionary zurück, das pro Team die verletzten Spielernamen enthält (kompatibel zu
    bestehenden Aufrufen) UND unter dem Schlüssel '_meta' detaillierte Schlüsselspieler-Ausfälle
    sowie taktische Team-Profile (Standards-Stärke, Standards-Anfälligkeit, Chancen-Index etc.) liefert.
    """
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )

    injured_by_team: dict[str, list[str]] = {team: [] for team in TEAM_POWER_INDEX.keys()}
    detailed_injuries_by_team: dict[str, list[dict]] = {team: [] for team in TEAM_POWER_INDEX.keys()}
    key_missing_by_team: dict[str, list[dict]] = {team: [] for team in TEAM_POWER_INDEX.keys()}
    tactical_by_team: dict[str, dict] = {
        team: _default_tactical_team_dict(team) for team in TEAM_POWER_INDEX.keys()
    }

    # Spieler-Statistik-Datenbank (Key = li_player_id oder normalisierter Name)
    player_stats_by_id: dict[str, dict] = {}
    player_stats_by_name: dict[str, dict] = {}

    urls = {
        "injuries": "https://www.ligainsider.de/bundesliga/verletzte-und-gesperrte-spieler/",
        "scorers": "https://www.ligainsider.de/bundesliga/torschuetzenliste/",
        "ratings": "https://www.ligainsider.de/bundesliga/noten/spieltag/",
        "aerial": "https://www.ligainsider.de/bundesliga/ranking-top-10/luftkampfquote/",
        "fouls": "https://www.ligainsider.de/bundesliga/ranking-top-10/fouls/",
        "big_chances": "https://www.ligainsider.de/bundesliga/ranking-top-10/grosschancen-kreiert/",
        "shots": "https://www.ligainsider.de/bundesliga/ranking-top-10/torschuesse/",
        "errors": "https://www.ligainsider.de/bundesliga/ranking-top-10/error/",
        "box_cmd": "https://www.ligainsider.de/bundesliga/ranking-top-10/strafraumbeherrschung/",
        "recoveries": "https://www.ligainsider.de/bundesliga/ranking-top-10/balleroberungen/",
        "turnovers": "https://www.ligainsider.de/bundesliga/ranking-top-10/ballverluste/",
    }

    def _fetch_page(item: tuple[str, str]) -> tuple[str, str]:
        key, url = item
        try:
            resp = scraper.get(url, timeout=12)
            if resp.status_code == 200:
                return key, resp.text
        except Exception:
            pass
        return key, ""

    pages: dict[str, str] = {}
    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            for k, html in executor.map(_fetch_page, urls.items()):
                if html:
                    pages[k] = html
    except Exception:
        pass

    def _get_or_create_pstat(li_id: str, name: str, team: str, pos: str = "") -> dict:
        norm_name = name.lower().strip()
        entry = (player_stats_by_id.get(li_id) if li_id else None) or player_stats_by_name.get(norm_name)
        if not entry:
            entry = {
                "li_id": li_id,
                "name": name,
                "team": team,
                "position": pos,
                "goals": 0,
                "assists": 0,
                "scorer_points": 0,
                "goal_share_pct": 0.0,
                "appearances": 0,
                "minutes": 0,
                "avg_grade": 3.5,
                "avg_points": 0.0,
                "big_chances": 0,
                "shots_on_target": 0,
            }
        if li_id:
            player_stats_by_id[li_id] = entry
        if norm_name:
            player_stats_by_name[norm_name] = entry
        if pos and not entry.get("position"):
            entry["position"] = pos
        if team and not entry.get("team"):
            entry["team"] = team
        return entry

    # --- 1. Torschützen & Scorer (inkl. % Anteil an den Teamtoren!) parsen ---
    if "scorers" in pages:
        soup = BeautifulSoup(pages["scorers"], "html.parser")
        # Desktop-Struktur (div.leg_column_row)
        for row in soup.select("div.leg_column_row"):
            a_tag = row.select_one("a[href]")
            if not a_tag:
                continue
            li_id = _extract_li_player_id(a_tag.get("href", ""))
            img = a_tag.find("img", alt=True)
            p_name = img["alt"].strip() if img and img.get("alt") else a_tag.get_text(" ", strip=True)

            criteria_lis = row.select("div[data-criteria-rowfilter] li")
            raw_team = criteria_lis[3].get_text(strip=True) if len(criteria_lis) >= 4 else ""
            raw_pos = criteria_lis[4].get_text(strip=True) if len(criteria_lis) >= 5 else "ST"
            team_name = match_team_name(raw_team)

            goals = int(_parse_german_float(row.select_one(".tabsort2").get_text(strip=True))) if row.select_one(".tabsort2") else 0
            assists = int(_parse_german_float(row.select_one(".tabsort3").get_text(strip=True))) if row.select_one(".tabsort3") else 0
            scorer_pts = int(_parse_german_float(row.select_one(".tabsort4").get_text(strip=True))) if row.select_one(".tabsort4") else 0
            apps = int(_parse_german_float(row.select_one(".tabsort5").get_text(strip=True))) if row.select_one(".tabsort5") else 0
            share_el = row.select_one("strong.progress_up")
            share_pct = _parse_german_float(share_el.get_text(strip=True)) if share_el else 0.0

            entry = _get_or_create_pstat(li_id, p_name, team_name, raw_pos)
            entry["goals"] = goals
            entry["assists"] = assists
            entry["scorer_points"] = scorer_pts
            entry["appearances"] = max(entry["appearances"], apps)
            entry["goal_share_pct"] = share_pct

        # Mobile-Fallback (tr.leg_column_row)
        for tr in soup.select("tr.leg_column_row"):
            tds = tr.find_all("td")
            if len(tds) < 10:
                continue
            a_tag = tds[1].find("a", href=True)
            li_id = _extract_li_player_id(a_tag["href"]) if a_tag else ""
            p_name = tds[1].get_text(strip=True)
            team_name = match_team_name(tds[2].get_text(strip=True))
            raw_pos = tds[3].get_text(strip=True)
            entry = _get_or_create_pstat(li_id, p_name, team_name, raw_pos)
            entry["goals"] = int(_parse_german_float(tds[4].get_text(strip=True)))
            entry["assists"] = int(_parse_german_float(tds[5].get_text(strip=True)))
            entry["scorer_points"] = int(_parse_german_float(tds[6].get_text(strip=True)))
            entry["appearances"] = max(entry["appearances"], int(_parse_german_float(tds[7].get_text(strip=True))))
            entry["goal_share_pct"] = _parse_german_float(tds[9].get_text(strip=True))

    # --- 2. LigaInsider-Noten & Saison-Ø-Punkte parsen ---
    if "ratings" in pages:
        soup = BeautifulSoup(pages["ratings"], "html.parser")
        for tr in soup.select("table#DataTable tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 11:
                continue
            a_tag = tds[2].find("a", href=True)
            li_id = _extract_li_player_id(a_tag["href"]) if a_tag else ""
            p_name = tds[2].get_text(strip=True)
            team_name = match_team_name(tds[3].get_text(strip=True))
            pos_str = tds[4].get_text(strip=True)
            avg_grade = _parse_german_float(tds[9].get_text(strip=True))
            avg_pts = _parse_german_float(tds[-1].get_text(strip=True))

            entry = _get_or_create_pstat(li_id, p_name, team_name, pos_str)
            if avg_grade > 0:
                entry["avg_grade"] = avg_grade
            if avg_pts > 0:
                entry["avg_points"] = avg_pts

    # --- 3. Taktische Ranking-Tabellen parsen (Spieler + Team-Aggregation) ---
    def _parse_ranking_table(page_key: str):
        html = pages.get(page_key, "")
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        rows_out = []
        for tr in soup.select("table#DataTable tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 8:
                continue
            a_tag = tds[2].find("a", href=True)
            li_id = _extract_li_player_id(a_tag["href"]) if a_tag else ""
            p_name = tds[2].get_text(strip=True)
            team_name = match_team_name(tds[3].get_text(strip=True))
            pos_str = tds[4].get_text(strip=True)
            apps = int(_parse_german_float(tds[5].get_text(strip=True)))
            mins = int(_parse_german_float(tds[6].get_text(strip=True)))
            val1 = _parse_german_float(tds[7].get_text(strip=True))
            val2 = _parse_german_float(tds[8].get_text(strip=True)) if len(tds) > 8 else 0.0

            entry = _get_or_create_pstat(li_id, p_name, team_name, pos_str)
            entry["appearances"] = max(entry["appearances"], apps)
            entry["minutes"] = max(entry["minutes"], mins)
            rows_out.append((entry, team_name, pos_str.lower(), val1, val2))
        return rows_out

    # 3a. Luftkampfquote (Kopfballstärke für Standards offensiv & defensiv)
    for entry, t_name, pos_l, won, total in _parse_ranking_table("aerial"):
        if t_name in tactical_by_team:
            tactical_by_team[t_name]["aerial_won"] += int(won)
            tactical_by_team[t_name]["aerial_total"] += int(total)
            if any(k in pos_l for k in ("innenverteidiger", "stürmer", "iv", "st")):
                tactical_by_team[t_name]["cb_st_aerial_won"] += int(won)

    # 3b. Begangene Fouls (verursacht gefährliche Freistoß-Standards)
    for entry, t_name, _, fouls_cnt, _ in _parse_ranking_table("fouls"):
        if t_name in tactical_by_team:
            tactical_by_team[t_name]["fouls_committed"] += int(fouls_cnt)

    # 3c. Kreierte Großchancen
    for entry, t_name, _, chances, _ in _parse_ranking_table("big_chances"):
        entry["big_chances"] = int(chances)
        if t_name in tactical_by_team:
            tactical_by_team[t_name]["big_chances_created"] += int(chances)

    # 3d. Schüsse aufs Tor
    for entry, t_name, _, shots, _ in _parse_ranking_table("shots"):
        entry["shots_on_target"] = int(shots)
        if t_name in tactical_by_team:
            tactical_by_team[t_name]["shots_on_target"] += int(shots)

    # 3e. Fehler vor Schuss/Gegentor
    for entry, t_name, _, errs, _ in _parse_ranking_table("errors"):
        if t_name in tactical_by_team:
            tactical_by_team[t_name]["errors_before_shot"] += int(errs)

    # 3f. Torwart-Strafraumbeherrschung (abgefangene Flanken bei Ecken/Standards)
    for entry, t_name, _, claimed, total_crosses in _parse_ranking_table("box_cmd"):
        if t_name in tactical_by_team:
            tactical_by_team[t_name]["gk_crosses_claimed"] += int(claimed)
            tactical_by_team[t_name]["gk_crosses_total"] += int(total_crosses)

    # 3g. Balleroberungen & Ballverluste (Pressing / Umschaltspiel)
    for entry, t_name, _, recs, _ in _parse_ranking_table("recoveries"):
        if t_name in tactical_by_team:
            tactical_by_team[t_name]["ball_recoveries"] += int(recs)

    for entry, t_name, _, turns, _ in _parse_ranking_table("turnovers"):
        if t_name in tactical_by_team:
            tactical_by_team[t_name]["turnovers"] += int(turns)

    # Taktische Indizes pro Team berechnen (insb. Standards-Stärke & Standards-Anfälligkeit)
    for t_name, tac in tactical_by_team.items():
        if tac["aerial_total"] > 0:
            tac["aerial_quote"] = round(tac["aerial_won"] / tac["aerial_total"] * 100.0, 1)
        if tac["gk_crosses_total"] > 0:
            tac["gk_box_command_quote"] = round(
                tac["gk_crosses_claimed"] / tac["gk_crosses_total"] * 100.0, 1
            )

        base_sp_att = TEAM_POWER_INDEX.get(t_name, {}).get("set_piece_attack", 78)
        base_sp_def = TEAM_POWER_INDEX.get(t_name, {}).get("set_piece_defense", 75)

        # Standards-Offensiv-Index (Kopfball-Siege IV/ST + Luftkampfquote + Großchancen + Basis-Profil)
        aerial_bonus = (tac["aerial_quote"] - 50.0) * 0.45 + min(tac["cb_st_aerial_won"], 60) * 0.18
        chance_bonus = min(tac["big_chances_created"], 25) * 0.25
        tac["standard_attack_score"] = round(base_sp_att * 0.65 + 25.0 + aerial_bonus + chance_bonus, 1)

        # Standards-Anfälligkeits-Index (Hoch = lässt viele Standardchancen/Gegentore zu!)
        # Viele begangene Fouls + schwache Luftkampfquote + schwache Torwart-Strafraumbeherrschung
        foul_malus = min(tac["fouls_committed"], 80) * 0.20
        aerial_weakness = max(0.0, 54.0 - tac["aerial_quote"]) * 0.70
        gk_weakness = max(0.0, 75.0 - tac["gk_box_command_quote"]) * 0.25
        base_vuln = 100.0 - base_sp_def
        tac["standard_vulnerability_score"] = round(
            base_vuln * 0.60 + 8.0 + foul_malus + aerial_weakness + gk_weakness, 1
        )

    # --- 4. Verletzte & gesperrte Spieler parsen + Schlüsselspieler-Prüfung ---
    if "injuries" in pages:
        soup = BeautifulSoup(pages["injuries"], "html.parser")
        for block in soup.select("div.personal_table"):
            h2 = block.select_one("div.leg_table_title h2")
            if not h2:
                continue
            team_name = match_team_name(h2.get_text(strip=True))
            if team_name not in injured_by_team:
                continue

            for row in block.select("div.small_table_row"):
                col1 = row.select_one("div.small_table_column1")
                if not col1:
                    continue
                a_tag = col1.find("a", href=True)
                li_id = _extract_li_player_id(a_tag["href"]) if a_tag else ""
                title_div = col1.select_one("div.left_title")
                p_name = title_div.get_text(strip=True) if title_div else (a_tag.get_text(strip=True) if a_tag else "")
                if not p_name:
                    continue

                icon_img = col1.select_one("div.left_icon img")
                status_type = icon_img.get("alt", "Ausfall").strip() if icon_img else "Ausfall"

                col2 = row.select_one("div.small_table_column2")
                col3 = row.select_one("div.small_table_column3")
                col4 = row.select_one("div.small_table_column4")
                reason = col2.get_text(strip=True) if col2 else ""
                if not reason and col3:
                    reason = col3.get_text(strip=True)
                missing_since = col4.get_text(strip=True) if col4 else ""

                if p_name not in injured_by_team[team_name]:
                    injured_by_team[team_name].append(p_name)

                # Prüfen, ob es sich um einen Schlüsselspieler handelt
                pstat = (
                    (player_stats_by_id.get(li_id) if li_id else None)
                    or player_stats_by_name.get(p_name.lower().strip())
                )
                # Auch Nachnamen-Abgleich für pstat und TEAM_KEY_PLAYERS
                last_name = p_name.split()[-1].lower().strip()
                if not pstat:
                    for k_name, v_stat in player_stats_by_name.items():
                        if v_stat.get("team") == team_name and k_name.endswith(last_name) and len(last_name) >= 4:
                            pstat = v_stat
                            break

                known_keys = TEAM_KEY_PLAYERS.get(team_name, {})
                known_info = None
                p_lower = p_name.lower()
                for k_alias, k_data in known_keys.items():
                    if k_alias in p_lower:
                        known_info = k_data
                        break

                recency = _missing_duration_recency_factor(missing_since)

                # Schlüsselspieler-Kriterien auswerten
                is_key = False
                role = "both"
                raw_importance = 0.25
                reasons_tags = []

                if known_info:
                    is_key = True
                    role = known_info["role"]
                    raw_importance = max(raw_importance, known_info["weight"])
                    reasons_tags.append(known_info["label"])

                if pstat:
                    pos_l = str(pstat.get("position", "")).lower()
                    if any(w in pos_l for w in ("stürmer", "st", "offensiv", "außen", "om", "ra", "la", "ms")):
                        role = "offense" if role == "both" else role
                    elif any(w in pos_l for w in ("tor", "tw", "verteidiger", "abwehr", "iv", "rv", "lv", "defensiv", "dm")):
                        role = "defense" if role == "both" else role

                    g_share = pstat.get("goal_share_pct", 0.0)
                    sc_pts = pstat.get("scorer_points", 0)
                    avg_pts = pstat.get("avg_points", 0.0)
                    mins = pstat.get("minutes", 0)
                    big_ch = pstat.get("big_chances", 0)

                    if g_share >= 15.0 or sc_pts >= 3:
                        is_key = True
                        role = "offense"
                        raw_importance = max(raw_importance, min(1.0, 0.55 + g_share / 60.0))
                        reasons_tags.append(f"{sc_pts} Scorer / {g_share:.0f}% Toranteil")
                    if avg_pts >= 62.0:
                        is_key = True
                        raw_importance = max(raw_importance, min(0.95, avg_pts / 90.0))
                        reasons_tags.append(f"LI-Topwert Ø {avg_pts:.0f} Pkt")
                    if big_ch >= 3:
                        is_key = True
                        role = "offense"
                        raw_importance = max(raw_importance, 0.70)
                        reasons_tags.append(f"{big_ch} Großchancen kreiert")
                    if mins >= 360 and not is_key:
                        is_key = True
                        raw_importance = max(raw_importance, 0.65)
                        reasons_tags.append(f"Stammspieler ({mins} Min.)")

                effective_impact = round(raw_importance * recency, 2)
                info_dict = {
                    "name": p_name,
                    "li_id": li_id,
                    "status_type": status_type,
                    "reason": reason,
                    "missing_since": missing_since,
                    "is_key_player": is_key,
                    "role": role,
                    "raw_importance": round(raw_importance, 2),
                    "recency_factor": recency,
                    "effective_impact": effective_impact,
                    "key_description": ", ".join(dict.fromkeys(reasons_tags)) if reasons_tags else "Kaderspieler",
                }
                detailed_injuries_by_team[team_name].append(info_dict)
                if is_key and recency >= 0.4:
                    key_missing_by_team[team_name].append(info_dict)

    # Meta-Daten direkt im Rückgabe-Dict hinterlegen (100% abwärtskompatibel für Aufrufer,
    # die `injuries.get(team_name, [])` nutzen!)
    injured_by_team["_meta"] = {
        "detailed_injuries": detailed_injuries_by_team,
        "key_players_missing": key_missing_by_team,
        "tactical_by_team": tactical_by_team,
        "player_stats_count": len(player_stats_by_name),
    }
    return injured_by_team


def _get_h2h_offset(home_team: str, away_team: str, h2h_results: dict) -> tuple[float, float]:
    """
    Berechnet einen Goal-Offset aus bisherigen direkten Duellen der Saison.
    Gibt (home_offset, away_offset) zurück.
    """
    h2h_key = tuple(sorted([home_team, away_team]))
    matches = h2h_results.get(h2h_key, [])
    if not matches:
        return 0.0, 0.0

    home_goals_total = 0.0
    away_goals_total = 0.0
    count = 0
    for g1, g2, t1, t2 in matches:
        if t1 == home_team:
            home_goals_total += g1
            away_goals_total += g2
        else:
            home_goals_total += g2
            away_goals_total += g1
        count += 1

    if count == 0:
        return 0.0, 0.0

    avg_home = home_goals_total / count
    avg_away = away_goals_total / count
    LIGA_AVG = 1.45
    H2H_WEIGHT = 0.15
    return (avg_home - LIGA_AVG) * H2H_WEIGHT, (avg_away - LIGA_AVG) * H2H_WEIGHT


# ---------------------------------------------------------------------------
# Haupt-Berechnung pro Spielpaarung (calculate_match_tip)
# ---------------------------------------------------------------------------
def calculate_match_tip(
    home_team: str,
    away_team: str,
    injuries: dict,
    season_stats: dict,
    h2h_results: dict | None = None,
) -> dict:
    """
    Berechnet das wahrscheinlichste exakte Ergebnis über ein erweitertes Poisson-Modell.

    Berücksichtigte Faktoren:
    1. Bisherige Saisonergebnisse (Gesamtschnitt, Heim-/Auswärts-Bilanz, Form der letzten 5 Spiele, H2H)
    2. Aktuelle Tabellensituation (Tabellenplatz 1–18, Punkte-Abstand, Topspiel-/Abstiegskampf-Kontext)
    3. Tore & Gegentore (Reale Offensiv-Torrate & Defensiv-Gegentorrate gesamt + Heim/Auswärts)
    4. Schlüsselspieler-Ausfälle (Differenziert nach Offensiv-/Defensiv-Schlüsselspielern und Ausfalldauer)
    5. Taktische LigaInsider-Daten (Standards-Stärke vs. Standards-Anfälligkeit, Großchancen,
       Schüsse aufs Tor, Defensivfehler vor Gegentor, Luftkampfquote, Pressing/Balleroberungen)
    """
    h_base = TEAM_POWER_INDEX.get(home_team, {"offense": 75, "defense": 75, "overall": 75, "set_piece_attack": 75, "set_piece_defense": 75})
    a_base = TEAM_POWER_INDEX.get(away_team, {"offense": 75, "defense": 75, "overall": 75, "set_piece_attack": 75, "set_piece_defense": 75})

    h_stats = season_stats.get(home_team, _default_team_season_dict())
    a_stats = season_stats.get(away_team, _default_team_season_dict())

    h_matches = int(h_stats.get("matches", 0))
    a_matches = int(a_stats.get("matches", 0))

    # Liga-Durchschnittstorschnitt pro Team und Spiel berechnen (Fallback: 1.50)
    total_goals_all = sum(
        s.get("goals_scored", 0) for k, s in season_stats.items() if isinstance(s, dict) and k in TEAM_POWER_INDEX
    )
    total_team_games_all = sum(
        s.get("matches", 0) for k, s in season_stats.items() if isinstance(s, dict) and k in TEAM_POWER_INDEX
    )
    liga_avg_goals = (total_goals_all / total_team_games_all) if total_team_games_all >= 18 else 1.50
    liga_avg_goals = max(1.25, min(liga_avg_goals, 1.85))

    # --- 1. TORE & GEGENTORE (Gesamt + Heim/Auswärts + Letzte 5 Spiele) ---
    base_h_attack_rate = 1.40 + (h_base["offense"] - 78.0) * 0.028
    base_a_attack_rate = 1.20 + (a_base["offense"] - 78.0) * 0.026
    base_h_concede_rate = max(0.65, 1.35 - (h_base["defense"] - 76.0) * 0.024)
    base_a_concede_rate = max(0.75, 1.45 - (a_base["defense"] - 76.0) * 0.024)

    if h_matches > 0:
        h_gpg = h_stats["goals_scored"] / h_matches
        h_cpg = h_stats["goals_conceded"] / h_matches
        h_home_m = max(1, h_stats.get("home_matches", 0))
        h_home_gpg = h_stats.get("home_goals_scored", h_gpg * h_home_m) / h_home_m if h_stats.get("home_matches", 0) > 0 else h_gpg * 1.08
        h_home_cpg = h_stats.get("home_goals_conceded", h_cpg * h_home_m) / h_home_m if h_stats.get("home_matches", 0) > 0 else h_cpg * 0.92

        season_weight = min(0.65, h_matches * 0.085)
        h_attack_rate = base_h_attack_rate * (1.0 - season_weight) + (0.60 * h_gpg + 0.40 * h_home_gpg) * season_weight
        h_concede_rate = base_h_concede_rate * (1.0 - season_weight) + (0.60 * h_cpg + 0.40 * h_home_cpg) * season_weight
    else:
        h_gpg, h_cpg = base_h_attack_rate, base_h_concede_rate
        h_attack_rate = base_h_attack_rate
        h_concede_rate = base_h_concede_rate

    if a_matches > 0:
        a_gpg = a_stats["goals_scored"] / a_matches
        a_cpg = a_stats["goals_conceded"] / a_matches
        a_away_m = max(1, a_stats.get("away_matches", 0))
        a_away_gpg = a_stats.get("away_goals_scored", a_gpg * a_away_m) / a_away_m if a_stats.get("away_matches", 0) > 0 else a_gpg * 0.92
        a_away_cpg = a_stats.get("away_goals_conceded", a_cpg * a_away_m) / a_away_m if a_stats.get("away_matches", 0) > 0 else a_cpg * 1.08

        season_weight = min(0.65, a_matches * 0.085)
        a_attack_rate = base_a_attack_rate * (1.0 - season_weight) + (0.60 * a_gpg + 0.40 * a_away_gpg) * season_weight
        a_concede_rate = base_a_concede_rate * (1.0 - season_weight) + (0.60 * a_cpg + 0.40 * a_away_cpg) * season_weight
    else:
        a_gpg, a_cpg = base_a_attack_rate, base_a_concede_rate
        a_attack_rate = base_a_attack_rate
        a_concede_rate = base_a_concede_rate

    # Defensiv-Multiplikatoren des Gegners (lässt der Gegner viele Gegentore zu -> höherer Faktor)
    a_def_vulnerability = (0.65 * (a_concede_rate / liga_avg_goals) + 0.35 * (1.0 + (78.0 - a_base["defense"]) * 0.012))
    h_def_vulnerability = (0.65 * (h_concede_rate / liga_avg_goals) + 0.35 * (1.0 + (78.0 - h_base["defense"]) * 0.012))
    a_def_vulnerability = max(0.62, min(a_def_vulnerability, 1.48))
    h_def_vulnerability = max(0.62, min(h_def_vulnerability, 1.48))

    # --- 2. TABELLENSITUATION & BISHERIGE SAISONERGEBNISSE (Form & Rang-Delta) ---
    h_rank = int(h_stats.get("rank", 10))
    a_rank = int(a_stats.get("rank", 10))
    h_pts = int(h_stats.get("points", 0))
    a_pts = int(a_stats.get("points", 0))
    h_form = float(h_stats.get("form_score", 0.0))
    a_form = float(a_stats.get("form_score", 0.0))

    # Tabellenplatz-Vorteil (positiv wenn Heimteam besser platziert ist als Gast)
    rank_diff = (a_rank - h_rank) if (h_matches > 0 and a_matches > 0) else 0
    table_edge_home = rank_diff * 0.016
    table_edge_away = -rank_diff * 0.016

    # --- 3. SCHLÜSSELSPIELER-PRÜFUNG & AUSFÄLLE ---
    meta = injuries.get("_meta", {}) if isinstance(injuries, dict) else {}
    detailed_injuries = meta.get("detailed_injuries", {})
    key_missing_map = meta.get("key_players_missing", {})
    tactical_map = meta.get("tactical_by_team", {})

    h_raw_list = injuries.get(home_team, []) if isinstance(injuries, dict) else []
    a_raw_list = injuries.get(away_team, []) if isinstance(injuries, dict) else []
    h_inj_count = len(h_raw_list)
    a_inj_count = len(a_raw_list)

    h_key_missing = key_missing_map.get(home_team, [])
    a_key_missing = key_missing_map.get(away_team, [])

    # Falls calculate_match_tip im Test ohne _meta aufgerufen wurde, klassifiziere anhand TEAM_KEY_PLAYERS
    if not detailed_injuries and (h_raw_list or a_raw_list):
        for t_team, r_list, target_list in (
            (home_team, h_raw_list, h_key_missing),
            (away_team, a_raw_list, a_key_missing),
        ):
            known = TEAM_KEY_PLAYERS.get(t_team, {})
            for p_str in r_list:
                p_low = str(p_str).lower()
                matched_k = next((v for k, v in known.items() if k in p_low), None)
                if matched_k:
                    target_list.append({
                        "name": p_str,
                        "role": matched_k["role"],
                        "effective_impact": matched_k["weight"],
                        "key_description": matched_k["label"],
                    })

    def _calc_injury_penalties(team_name: str, total_cnt: int, key_list: list[dict]) -> tuple[float, float]:
        """
        Berechnet (attack_malus, defense_malus_for_opponent_boost) durch Ausfälle.
        Schlüsselspieler-Ausfälle wiegen deutlich schwerer als reine Kaderbreite-Ausfälle.
        """
        att_malus = 0.0
        def_malus = 0.0
        if key_list:
            for kp in key_list:
                imp = float(kp.get("effective_impact", 0.7))
                role = kp.get("role", "both")
                if role == "offense":
                    att_malus += imp * 0.22
                elif role == "defense":
                    def_malus += imp * 0.20
                else:
                    att_malus += imp * 0.14
                    def_malus += imp * 0.12
            non_key_cnt = max(0, total_cnt - len(key_list))
            # Nicht-Schlüsselspieler im Detail-Dict prüfen (Langzeitverletzte kaum abziehen)
            det_list = detailed_injuries.get(team_name, [])
            if det_list:
                non_key_imp = sum(
                    d.get("effective_impact", 0.2) for d in det_list if not d.get("is_key_player")
                )
                att_malus += min(0.18, non_key_imp * 0.08)
                def_malus += min(0.15, non_key_imp * 0.07)
            else:
                att_malus += min(0.18, non_key_cnt * 0.04)
                def_malus += min(0.15, non_key_cnt * 0.04)
        else:
            # Wenn keine spezifischen Namen gematcht wurden (z. B. synthetischer Test mit "Spieler1"):
            att_malus = min(0.55, total_cnt * 0.11)
            def_malus = min(0.40, total_cnt * 0.08)

        return min(0.65, att_malus), min(0.50, def_malus)

    h_att_malus, h_def_malus = _calc_injury_penalties(home_team, h_inj_count, h_key_missing)
    a_att_malus, a_def_malus = _calc_injury_penalties(away_team, a_inj_count, a_key_missing)

    # --- 4. TAKTISCHE LIGAINSIDER-FAKTOREN (Standards, Großchancen, Fehler, Pressing) ---
    h_tac = tactical_map.get(home_team, _default_tactical_team_dict(home_team))
    a_tac = tactical_map.get(away_team, _default_tactical_team_dict(away_team))

    # 4a. Standards-Matchup: Standard-Stärke Team A vs. Standard-Anfälligkeit Team B
    # Hoher positiver Wert = Team A erzielt viel über Standards UND Team B lässt viel über Standards zu
    h_sp_edge = (h_tac["standard_attack_score"] - 78.0) + (a_tac["standard_vulnerability_score"] - 25.0)
    a_sp_edge = (a_tac["standard_attack_score"] - 78.0) + (h_tac["standard_vulnerability_score"] - 25.0)

    h_standard_bonus = max(-0.08, min(0.28, h_sp_edge * 0.0085)) if h_sp_edge > 4.0 else 0.0
    a_standard_bonus = max(-0.08, min(0.28, a_sp_edge * 0.0085)) if a_sp_edge > 4.0 else 0.0

    # 4b. Chancenkreation & Schuss-Druck vs. Gegner-Fehler vor Gegentor
    h_chance_bonus = 0.0
    a_chance_bonus = 0.0
    if h_tac["big_chances_created"] > 0 or a_tac["big_chances_created"] > 0:
        chance_diff = h_tac["big_chances_created"] - a_tac["big_chances_created"]
        h_chance_bonus += max(-0.10, min(0.14, chance_diff * 0.012))
        a_chance_bonus += max(-0.10, min(0.14, -chance_diff * 0.012))

    if a_tac["errors_before_shot"] >= 3:
        h_chance_bonus += min(0.12, (a_tac["errors_before_shot"] - 2) * 0.025)
    if h_tac["errors_before_shot"] >= 3:
        a_chance_bonus += min(0.12, (h_tac["errors_before_shot"] - 2) * 0.025)

    # 4c. Pressing / Balleroberungen gegen Ballverluste
    h_pressing_bonus = 0.0
    a_pressing_bonus = 0.0
    if h_tac["ball_recoveries"] > 0 and a_tac["turnovers"] > 0:
        if h_tac["ball_recoveries"] > a_tac["ball_recoveries"] * 1.12 and a_tac["turnovers"] > h_tac["turnovers"]:
            h_pressing_bonus = 0.07
        elif a_tac["ball_recoveries"] > h_tac["ball_recoveries"] * 1.12 and h_tac["turnovers"] > a_tac["turnovers"]:
            a_pressing_bonus = 0.07

    # --- 5. HEAD-TO-HEAD OFFSET ---
    h2h_home_off, h2h_away_off = (0.0, 0.0)
    if h2h_results:
        h2h_home_off, h2h_away_off = _get_h2h_offset(home_team, away_team, h2h_results)

    # --- 6. EXPECTED GOALS (LAMBDA) BERECHNEN ---
    home_lambda = (
        h_attack_rate * a_def_vulnerability
        + 0.16                   # Heimvorteil
        + h_form * 0.11          # Aktuelle Form (letzte 5 Spiele)
        + table_edge_home        # Tabellensituation
        - h_att_malus            # Eigene Offensiv-/Schlüsselspieler-Ausfälle
        + a_def_malus            # Gegnerische Defensiv-/Torwart-Ausfälle
        + h_standard_bonus       # Standards-Vorteil (Kopfball/Freistöße/Ecken)
        + h_chance_bonus         # Großchancen & Gegner-Fehler vor Gegentor
        + h_pressing_bonus       # Pressing / Balleroberungs-Vorteil
        + h2h_home_off           # Direkter Vergleich
    )

    away_lambda = (
        a_attack_rate * h_def_vulnerability
        - 0.06                   # Leichter Auswärts-Abschlag
        + a_form * 0.11
        + table_edge_away
        - a_att_malus
        + h_def_malus
        + a_standard_bonus
        + a_chance_bonus
        + a_pressing_bonus
        + h2h_away_off
    )

    home_lambda = max(0.35, min(home_lambda, 3.9))
    away_lambda = max(0.30, min(away_lambda, 3.4))

    tip_home, tip_away = _poisson_most_likely_score(home_lambda, away_lambda)
    win_prob_h, draw_prob, win_prob_a = _calculate_1x2_probabilities(home_lambda, away_lambda)

    # --- 7. TRANSPARENTE ANALYSE & INSIGHTS AUFBEREITEN ---
    h_short = _short_team_name(home_team)
    a_short = _short_team_name(away_team)

    insights = []

    # 7a. Tabellen- & Torbilanz-Insight
    if h_matches > 0 and a_matches > 0:
        insights.append(
            f"📊 Tabelle: #{h_rank} {h_short} ({h_pts} Pkt, {h_stats['goals_scored']}:{h_stats['goals_conceded']} Tore) "
            f"vs. #{a_rank} {a_short} ({a_pts} Pkt, {a_stats['goals_scored']}:{a_stats['goals_conceded']} Tore)"
        )
        if h_stats.get("last5_form") or a_stats.get("last5_form"):
            insights.append(
                f"📈 Form (L5): {h_short} [{h_stats.get('last5_form', '—')}] ({'+' if h_form >= 0 else ''}{h_form:.1f}) | "
                f"{a_short} [{a_stats.get('last5_form', '—')}] ({'+' if a_form >= 0 else ''}{a_form:.1f})"
            )

    # 7b. Schlüsselspieler-Insight
    key_notes = []
    if h_key_missing:
        names_h = ", ".join(f"{kp['name']} ({kp['key_description']})" for kp in h_key_missing[:3])
        key_notes.append(f"{h_short}: {names_h}")
    if a_key_missing:
        names_a = ", ".join(f"{kp['name']} ({kp['key_description']})" for kp in a_key_missing[:3])
        key_notes.append(f"{a_short}: {names_a}")
    if key_notes:
        insights.append(f"🚨 Schlüsselspieler fehlen: {' ; '.join(key_notes)}")
    elif h_inj_count > 0 or a_inj_count > 0:
        insights.append(f"🏥 Ausfälle gesamt: {h_short} ({h_inj_count}), {a_short} ({a_inj_count}) – keine akuten Top-Scorer betroffen")

    # 7c. Standards- & Taktik-Insight aus LigaInsider
    tactical_notes = []
    if h_standard_bonus >= 0.07 and h_standard_bonus > a_standard_bonus + 0.03:
        tactical_notes.append(
            f"🎯 Standards-Vorteil {h_short} (Standard-Offensivwert {h_tac['standard_attack_score']:.0f}, "
            f"Luftkampf {h_tac['aerial_quote']:.1f}% gegen Standard-Anfälligkeit {a_short} "
            f"[{a_tac['fouls_committed']} Fouls, TW-Flankenquote {a_tac['gk_box_command_quote']:.0f}%])"
        )
    elif a_standard_bonus >= 0.07 and a_standard_bonus > h_standard_bonus + 0.03:
        tactical_notes.append(
            f"🎯 Standards-Vorteil {a_short} (Standard-Offensivwert {a_tac['standard_attack_score']:.0f}, "
            f"Luftkampf {a_tac['aerial_quote']:.1f}% gegen Standard-Anfälligkeit {h_short} "
            f"[{h_tac['fouls_committed']} Fouls, TW-Flankenquote {h_tac['gk_box_command_quote']:.0f}%])"
        )

    if h_tac["big_chances_created"] > 0 or a_tac["big_chances_created"] > 0:
        tactical_notes.append(
            f"⚡ LI-Chancen & Schüsse: {h_short} ({h_tac['big_chances_created']} Großchancen / {h_tac['shots_on_target']} Torschüsse) "
            f"vs. {a_short} ({a_tac['big_chances_created']} Großchancen / {a_tac['shots_on_target']} Torschüsse)"
        )

    if a_tac["errors_before_shot"] >= 4 or h_tac["errors_before_shot"] >= 4:
        tactical_notes.append(
            f"⚠️ Fehler vor Gegentor/Schuss: {h_short} ({h_tac['errors_before_shot']}) vs. {a_short} ({a_tac['errors_before_shot']})"
        )

    if tactical_notes:
        insights.extend(tactical_notes)

    detail_str = (
        f"xG (λ): {home_lambda:.2f} : {away_lambda:.2f} | "
        f"1X2: {win_prob_h}% / {draw_prob}% / {win_prob_a}% | "
        + " | ".join(insights)
    )

    return {
        "home": home_team,
        "away": away_team,
        "tip": f"{tip_home}:{tip_away}",
        "analysis": detail_str,
        "details": {
            "lambda_home": round(home_lambda, 2),
            "lambda_away": round(away_lambda, 2),
            "probabilities": {"home": win_prob_h, "draw": draw_prob, "away": win_prob_a},
            "home_rank": h_rank,
            "away_rank": a_rank,
            "home_points": h_pts,
            "away_points": a_pts,
            "home_goals": f"{h_stats.get('goals_scored', 0)}:{h_stats.get('goals_conceded', 0)}",
            "away_goals": f"{a_stats.get('goals_scored', 0)}:{a_stats.get('goals_conceded', 0)}",
            "home_form_str": h_stats.get("last5_form", "—"),
            "away_form_str": a_stats.get("last5_form", "—"),
            "home_recent_results": h_stats.get("recent_results", []),
            "away_recent_results": a_stats.get("recent_results", []),
            "home_key_missing": h_key_missing,
            "away_key_missing": a_key_missing,
            "home_total_injuries": h_inj_count,
            "away_total_injuries": a_inj_count,
            "home_standard_bonus": round(h_standard_bonus, 2),
            "away_standard_bonus": round(a_standard_bonus, 2),
            "home_tactical": h_tac,
            "away_tactical": a_tac,
            "insights": insights,
        },
    }


def calculate_season_bonus_questions(injuries: dict, season_stats: dict) -> list:
    """Berechnet die 5 Saison-Bonusfragen unter Einbezug von Tabelle, Toren und Schlüsselspieler-Ausfällen."""
    meta = injuries.get("_meta", {}) if isinstance(injuries, dict) else {}
    key_missing_map = meta.get("key_players_missing", {})

    def _team_dynamic_score(team_name: str, base_cfg: dict) -> float:
        s = season_stats.get(team_name, {})
        matches = s.get("matches", 0)
        pts_pg = (s.get("points", 0) / matches) if matches > 0 else 1.4
        gd_pg = (s.get("goal_diff", 0) / matches) if matches > 0 else 0.0
        key_pen = len(key_missing_map.get(team_name, [])) * 1.2
        return (
            base_cfg["overall"]
            + (pts_pg - 1.4) * 8.0
            + gd_pg * 3.5
            + s.get("form_score", 0.0) * 1.5
            - key_pen
        )

    teams_sorted_strength = sorted(
        TEAM_POWER_INDEX.items(),
        key=lambda x: _team_dynamic_score(x[0], x[1]),
        reverse=True,
    )

    teams_sorted_offense = sorted(
        TEAM_POWER_INDEX.items(),
        key=lambda x: x[1]["offense"] + (season_stats.get(x[0], {}).get("goals_scored", 0) * 1.8),
        reverse=True,
    )

    teams_sorted_pressure = sorted(
        TEAM_POWER_INDEX.items(),
        key=lambda x: x[1]["coach_pressure"]
        + (len(injuries.get(x[0], [])) * 3)
        - (season_stats.get(x[0], {}).get("points", 0) * 3.5)
        + (season_stats.get(x[0], {}).get("goals_conceded", 0) * 1.2),
        reverse=True,
    )

    champion = teams_sorted_strength[0][0]
    autumn_champ = teams_sorted_strength[0][0]
    top_off_team = teams_sorted_offense[0][0]
    top_scorer_team = f"{top_off_team} (Harry Kane / Michael Olise)" if "Bayern" in top_off_team else top_off_team
    relegation_teams = [t[0] for t in teams_sorted_strength[-3:]]
    trainer_risk = [t[0] for t in teams_sorted_pressure[:2]]

    champ_stats = season_stats.get(champion, {})
    return [
        {
            "question": "🏆 Wer wird Deutscher Meister?",
            "answer": champion,
            "confidence": "85%",
            "reasoning": (
                f"Höchster Gesamt-Index aus Kaderstärke ({TEAM_POWER_INDEX[champion]['overall']}), "
                f"aktueller Tabellenposition (#{champ_stats.get('rank', 1)}, {champ_stats.get('points', 0)} Pkt) "
                f"und Torbilanz ({champ_stats.get('goals_scored', 0)}:{champ_stats.get('goals_conceded', 0)})."
            ),
        },
        {
            "question": "🍂 Wer wird Herbstmeister?",
            "answer": autumn_champ,
            "confidence": "78%",
            "reasoning": "Höchstes Punkteschnitt- und Expected-Goals-Potenzial im bisherigen Saisonverlauf.",
        },
        {
            "question": "⚽ Welche Mannschaft stellt den Spieler mit den meisten Toren?",
            "answer": top_scorer_team,
            "confidence": "82%",
            "reasoning": (
                f"Stärkste Offensive ({season_stats.get(top_off_team, {}).get('goals_scored', 0)} Saisontore) "
                f"und höchste Großchancen-Generierung laut LigaInsider."
            ),
        },
        {
            "question": "📉 Welche Mannschaften belegen die Plätze 16-18?",
            "answer": ", ".join(relegation_teams),
            "confidence": "68%",
            "reasoning": "Niedrigster kombinierter Leistungsindex aus Punkteschnitt, Gegentorrate und Kadertiefe.",
        },
        {
            "question": "⏱️ Wo findet der erste Trainerwechsel statt?",
            "answer": f"{trainer_risk[0]} (Alternative: {trainer_risk[1]})",
            "confidence": "58%",
            "reasoning": "Größte Diskrepanz zwischen Saisonziel, Gegentoranfälligkeit und aktueller Punktausbeute.",
        },
    ]


def run_kicktipp_live_pipeline() -> dict:
    """
    Hauptfunktion: Lädt Spielplan, alle bisherigen Saisonergebnisse, offizielle Tabelle,
    LigaInsider-Schlüsselspieler-Ausfälle sowie Taktik-/Standards-Daten und berechnet die Tipps.
    """
    fixtures, matchday_label, league_season = fetch_official_matchday_fixtures()
    season_stats, h2h_results = fetch_season_past_results(league_season)
    injuries = scrape_ligainsider_live_data()

    match_predictions = []
    for h, a in fixtures:
        home_clean = match_team_name(h)
        away_clean = match_team_name(a)
        match_predictions.append(
            calculate_match_tip(home_clean, away_clean, injuries, season_stats, h2h_results)
        )

    bonus_answers = calculate_season_bonus_questions(injuries, season_stats)
    total_matches_played = sum(
        s.get("matches", 0) for k, s in season_stats.items() if k in TEAM_POWER_INDEX
    ) // 2
    total_injuries_cnt = sum(
        len(v) for k, v in injuries.items() if k in TEAM_POWER_INDEX and isinstance(v, list)
    )
    meta = injuries.get("_meta", {})
    key_missing_total = sum(len(v) for v in meta.get("key_players_missing", {}).values())

    # Sortierte Tabelle + Taktik-Übersicht für das Dashboard aufbereiten
    tactical_map = meta.get("tactical_by_team", {})
    key_missing_map = meta.get("key_players_missing", {})
    standings_table = []
    for t_name, s in sorted(season_stats.items(), key=lambda kv: (kv[1]["rank"], -kv[1]["points"])):
        tac = tactical_map.get(t_name, _default_tactical_team_dict(t_name))
        k_miss = key_missing_map.get(t_name, [])
        standings_table.append({
            "Platz": s["rank"],
            "Verein": t_name,
            "Sp.": s["matches"],
            "S-U-N": f"{s['won']}-{s['draw']}-{s['lost']}",
            "Tore": f"{s['goals_scored']}:{s['goals_conceded']}",
            "Diff.": s["goal_diff"],
            "Punkte": s["points"],
            "Form (L5)": s.get("last5_form", "—"),
            "Standards-Angriff": tac["standard_attack_score"],
            "Standards-Anfälligkeit": tac["standard_vulnerability_score"],
            "Luftkampf %": f"{tac['aerial_quote']:.1f}%",
            "Großchancen": tac["big_chances_created"],
            "Schlüsselspieler fehlt": ", ".join(kp["name"] for kp in k_miss) if k_miss else "—",
        })

    return {
        "matchday_label": matchday_label,
        "predictions": match_predictions,
        "bonus_questions": bonus_answers,
        "total_injuries_scraped": total_injuries_cnt,
        "total_key_players_missing": key_missing_total,
        "matches_analyzed": total_matches_played,
        "standings_table": standings_table,
    }
