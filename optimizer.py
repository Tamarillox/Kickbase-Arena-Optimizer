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
) -> tuple[float, list[int], int] | None:
    n = len(players)
    if n < 11:
        return None

    # Vorab-Check: Gibt es überhaupt genug Spieler auf jeder Position für diese Formation?
    positions_available = players["position"].value_counts().to_dict()
    for pos, required_count in formation.items():
        if positions_available.get(pos, 0) < required_count:
            return None  # Formation mangels Spielern nicht lösbar

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
) -> dict:
    if players.empty:
        raise OptimizationError("Keine Spieler für die Optimierung verfügbar.")

    # Gesperrte Spieler zuverlässig als String matchen und entfernen
    df = players.copy()
    df["id"] = df["id"].astype(str).str.strip()

    if excluded_player_ids:
        blocked = {str(pid).strip() for pid in excluded_player_ids}
        df = df[~df["id"].isin(blocked)].copy()

    df = df.reset_index(drop=True)

    if len(df) < 11:
        raise OptimizationError("Zu viele Spieler gesperrt. Mindestens 11 Spieler erforderlich.")

    formations_to_try = VALID_FORMATIONS if formation == "auto" else {formation: VALID_FORMATIONS[formation]}

    best_result = None
    best_formation_name = None
    best_obj = -float("inf")
    formation_scores = {}

    for fname, fconfig in formations_to_try.items():
        result = _solve_for_formation(df, budget, fconfig, fname)
        if result:
            formation_scores[fname] = round(result[0], 1)
            if result[0] > best_obj:
                best_obj = result[0]
                best_result = result
                best_formation_name = fname

    if best_result is None:
        raise OptimizationError(
            "Keine gültige Aufstellung gefunden. Bitte prüfe das Budget oder hebe einige Spielersperren auf."
        )

    obj_value, selected_indices, captain_idx = best_result
    lineup_df = df.iloc[selected_indices].copy()
    lineup_df["is_captain"] = lineup_df.index == captain_idx
    lineup_df = lineup_df.sort_values("position").reset_index(drop=True)

    total_mv = lineup_df["market_value"].sum()
    captain_row = df.iloc[captain_idx]
    total_xp = lineup_df["xp"].sum() + captain_row["xp"]

    return {
        "lineup": lineup_df,
        "captain_id": str(captain_row.get("id", "")),
        "captain_name": captain_row["name"],
        "formation": best_formation_name,
        "total_xp": round(total_xp, 2),
        "total_market_value": total_mv,
        "budget_remaining": budget - total_mv,
        "all_formation_scores": formation_scores,  # Zeigt den Vergleich aller Formationen
    }