"""
optimizer.py – ILP-basierte Aufstellungs-Optimierung für Kickbase Arena
"""

import pandas as pd
from pulp import (
    LpMaximize,
    LpProblem,
    LpVariable,
    lpSum,
    LpStatus,
    PULP_CBC_CMD,
)

VALID_FORMATIONS: dict[str, dict[int, int]] = {
    "3-4-3": {1: 1, 2: 3, 3: 4, 4: 3},
    "3-5-2": {1: 1, 2: 3, 3: 5, 4: 2},
    "4-3-3": {1: 1, 2: 4, 3: 3, 4: 3},
    "4-4-2": {1: 1, 2: 4, 3: 4, 4: 2},
    "4-5-1": {1: 1, 2: 4, 3: 5, 4: 1},
    "5-3-2": {1: 1, 2: 5, 3: 3, 4: 2},
    "5-4-1": {1: 1, 2: 5, 3: 4, 4: 1},
}


def get_valid_formations() -> dict[str, dict[int, int]]:
    return VALID_FORMATIONS.copy()


class OptimizationError(Exception):
    pass


def _solve_for_formation(
    players: pd.DataFrame,
    budget: float,
    formation: dict[int, int],
    formation_name: str,
    included_indices: list[int] | None = None,
) -> tuple[float, list[int], int] | None:
    n = len(players)
    if n < 11:
        return None

    included_indices = included_indices or []

    # Vorab-Check: Gibt es überhaupt genug Spieler auf jeder Position für diese Formation?
    positions_available = players["position"].value_counts().to_dict()
    for pos, required_count in formation.items():
        if positions_available.get(pos, 0) < required_count:
            return None  # Formation mangels Spielern nicht lösbar

    # Vorab-Check: Überschreiten die gehaltenen Spieler bereits die Positions-Limits dieser Formation?
    if included_indices:
        inc_positions = players.iloc[included_indices]["position"].value_counts().to_dict()
        for pos, inc_count in inc_positions.items():
            if inc_count > formation.get(pos, 0):
                return None

    prob = LpProblem(f"Kickbase_Arena_{formation_name}", LpMaximize)

    x = [LpVariable(f"x_{i}", cat="Binary") for i in range(n)]
    c = [LpVariable(f"c_{i}", cat="Binary") for i in range(n)]

    xp_values = players["xp"].values
    market_values = players["market_value"].values
    positions = players["position"].values
    teams = players["team"].values

    # Zielfunktion: Summe xP + Kapitän-Bonus
    prob += lpSum(xp_values[i] * x[i] + xp_values[i] * c[i] for i in range(n))

    # Constraints
    prob += lpSum(x[i] for i in range(n)) == 11
    prob += lpSum(market_values[i] * x[i] for i in range(n)) <= budget

    # Gehaltene / Fixierte Spieler müssen aufgestellt werden
    for idx in included_indices:
        prob += x[idx] == 1

    # Max. 3 Spieler pro Verein
    for team in set(teams):
        team_indices = [i for i in range(n) if teams[i] == team]
        if team_indices:
            prob += lpSum(x[i] for i in team_indices) <= 3

    # Exakt 1 Kapitän (der auch aufgestellt sein muss)
    prob += lpSum(c[i] for i in range(n)) == 1
    for i in range(n):
        prob += c[i] <= x[i]

    # Formationseinschränkungen
    for pos, count in formation.items():
        pos_indices = [i for i in range(n) if positions[i] == pos]
        prob += lpSum(x[i] for i in pos_indices) == count

    solver = PULP_CBC_CMD(msg=False, timeLimit=15)
    prob.solve(solver)

    if LpStatus[prob.status] != "Optimal":
        return None

    selected = [i for i in range(n) if x[i].varValue and x[i].varValue > 0.5]
    captain = next((i for i in range(n) if c[i].varValue and c[i].varValue > 0.5), selected[0])
    obj_value = prob.objective.value() if prob.objective.value() else 0.0

    return (obj_value, selected, captain)


def optimize_lineup(
    players: pd.DataFrame,
    budget: float = 150_000_000,
    formation: str = "auto",
    excluded_player_ids: list[str] | None = None,
    included_player_ids: list[str] | None = None,
) -> dict:
    if players.empty:
        raise OptimizationError("Keine Spieler für die Optimierung verfügbar.")

    df = players.copy()
    df["id"] = df["id"].astype(str).str.strip()

    included = {str(pid).strip() for pid in (included_player_ids or []) if str(pid).strip()}
    blocked = {str(pid).strip() for pid in (excluded_player_ids or []) if str(pid).strip()} - included

    if blocked:
        df = df[~df["id"].isin(blocked)].copy()

    df = df.reset_index(drop=True)

    if len(df) < 11:
        raise OptimizationError("Zu viele Spieler gesperrt. Mindestens 11 Spieler erforderlich.")

    # Gehaltene Spieler validieren
    included_indices: list[int] = []
    if included:
        inc_mask = df["id"].isin(included)
        inc_df = df[inc_mask]
        if len(inc_df) > 11:
            raise OptimizationError(
                f"Es sind {len(inc_df)} Spieler als 'gehalten' markiert – maximal 11 sind möglich."
            )
        inc_mv = float(inc_df["market_value"].sum())
        if inc_mv > budget:
            raise OptimizationError(
                f"Die gehaltenen Spieler kosten bereits {inc_mv / 1_000_000:.1f} Mio. € "
                f"und überschreiten das Budget ({budget / 1_000_000:.1f} Mio. €)."
            )
        club_counts = inc_df["team"].value_counts()
        if not club_counts.empty and club_counts.iloc[0] > 3:
            bad_club = club_counts.index[0]
            raise OptimizationError(
                f"Maximal 3 Spieler pro Verein erlaubt! Du hältst aktuell {club_counts.iloc[0]} Spieler von {bad_club}."
            )
        included_indices = inc_df.index.tolist()

    formations_to_try = VALID_FORMATIONS if formation == "auto" else {formation: VALID_FORMATIONS[formation]}

    best_result = None
    best_formation_name = None
    best_obj = -float("inf")
    formation_scores = {}

    for fname, fconfig in formations_to_try.items():
        result = _solve_for_formation(df, budget, fconfig, fname, included_indices=included_indices)
        if result:
            formation_scores[fname] = round(result[0], 1)
            if result[0] > best_obj:
                best_obj = result[0]
                best_result = result
                best_formation_name = fname

    if best_result is None:
        if included:
            raise OptimizationError(
                "Keine gültige Aufstellung mit den gehaltenen Spielern gefunden. "
                "Prüfe das Restbudget, die Formation (Tipp: 'auto' wählen) oder löse einzelne gehaltene/gesperrte Spieler."
            )
        raise OptimizationError(
            "Keine gültige Aufstellung gefunden. Bitte prüfe das Budget oder hebe einige Spielersperren auf."
        )

    obj_value, selected_indices, captain_idx = best_result
    lineup_df = df.iloc[selected_indices].copy()
    lineup_df["is_captain"] = lineup_df.index == captain_idx
    lineup_df["is_pinned"] = lineup_df["id"].isin(included)
    lineup_df = lineup_df.sort_values("position").reset_index(drop=True)

    total_mv = lineup_df["market_value"].sum()
    captain_row = df.iloc[captain_idx]
    total_xp = lineup_df["xp"].sum() + captain_row["xp"]

    return {
        "lineup": lineup_df,
        "captain_id": str(captain_row.get("id", "")),
        "captain_name": captain_row["name"],
        "formation": best_formation_name,
        "total_xp": round(float(total_xp), 2),
        "total_market_value": float(total_mv),
        "budget_remaining": float(budget - total_mv),
        "all_formation_scores": formation_scores,
    }


def evaluate_manual_lineup(
    players: pd.DataFrame,
    selected_player_ids: list[str],
    budget: float = 150_000_000,
    captain_id: str | None = None,
    pinned_player_ids: list[str] | None = None,
) -> dict:
    """
    Erstellt ein Aufstellungs-Ergebnis direkt aus 11 manuell ausgewählten Spielern.
    """
    if players.empty:
        raise OptimizationError("Keine Spielerdaten vorhanden.")

    df = players.copy()
    df["id"] = df["id"].astype(str).str.strip()
    if "xp" not in df.columns:
        df["xp"] = df.get("average_points", 0.0) * df.get("p_start", 1.0)

    sel_ids = [str(pid).strip() for pid in selected_player_ids if str(pid).strip()]
    lineup_df = df[df["id"].isin(set(sel_ids))].copy()
    lineup_df = lineup_df.drop_duplicates(subset=["id"]).reset_index(drop=True)

    if len(lineup_df) != 11:
        raise OptimizationError(
            f"Eine manuelle Aufstellung benötigt genau 11 Spieler (aktuell ausgewählt: {len(lineup_df)}). "
            f"Tipp: Nutze 'Gewählte Spieler halten & Rest optimal auffüllen', wenn du weniger als 11 Spieler vorgibst!"
        )

    pos_counts = lineup_df["position"].value_counts().to_dict()
    n_tw = pos_counts.get(1, 0)
    n_abw = pos_counts.get(2, 0)
    n_mit = pos_counts.get(3, 0)
    n_st = pos_counts.get(4, 0)

    if n_tw != 1:
        raise OptimizationError(f"Die Aufstellung muss genau 1 Torwart enthalten (aktuell: {n_tw}).")

    formation_str = f"{n_abw}-{n_mit}-{n_st}"
    if formation_str not in VALID_FORMATIONS:
        valid_str = ", ".join(VALID_FORMATIONS.keys())
        raise OptimizationError(
            f"Ungültige Formation {formation_str} ({n_tw} TW, {n_abw} ABW, {n_mit} MIT, {n_st} ST). "
            f"Erlaubte Formationen: {valid_str}."
        )

    club_counts = lineup_df["team"].value_counts()
    if not club_counts.empty and club_counts.iloc[0] > 3:
        bad_club = club_counts.index[0]
        raise OptimizationError(
            f"Maximal 3 Spieler pro Verein erlaubt! Du hast {club_counts.iloc[0]} Spieler von {bad_club} gewählt."
        )

    total_mv = float(lineup_df["market_value"].sum())
    if total_mv > budget:
        raise OptimizationError(
            f"Deine manuelle Aufstellung kostet {total_mv / 1_000_000:.1f} Mio. € "
            f"und überschreitet das Budget von {budget / 1_000_000:.1f} Mio. €."
        )

    # Kapitän bestimmen (manuell gewählt oder bester xP-Spieler)
    cap_id_clean = str(captain_id).strip() if captain_id else ""
    if cap_id_clean and cap_id_clean in set(lineup_df["id"]):
        cap_idx = lineup_df[lineup_df["id"] == cap_id_clean].index[0]
    else:
        cap_idx = lineup_df["xp"].idxmax()

    pinned_set = {str(pid).strip() for pid in (pinned_player_ids or sel_ids)}
    lineup_df["is_captain"] = lineup_df.index == cap_idx
    lineup_df["is_pinned"] = lineup_df["id"].isin(pinned_set)
    captain_row = lineup_df.loc[cap_idx]
    total_xp = float(lineup_df["xp"].sum() + captain_row["xp"])

    lineup_df = lineup_df.sort_values("position").reset_index(drop=True)

    return {
        "lineup": lineup_df,
        "captain_id": str(captain_row.get("id", "")),
        "captain_name": captain_row["name"],
        "formation": formation_str,
        "total_xp": round(total_xp, 2),
        "total_market_value": total_mv,
        "budget_remaining": float(budget - total_mv),
        "all_formation_scores": {formation_str: round(total_xp, 1)},
    }