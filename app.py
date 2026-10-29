"""
app.py – Streamlit Dashboard für den Kickbase Arena Optimizer & KickTipp
Features:
- Kickbase Lineup Optimization mit ILP-Solver (Arena-Marktwerte)
- Einzelne Spieler halten (Fixieren) & Rest neu suchen
- Team komplett selbst aufstellen (Manueller Team-Builder)
- Spieler-Sperrliste (Sperren & Re-Optimize)
- Formations-Vergleich
- Account-gebundene permanente Marktwert-Korrekturen (gespeichert in custom_market_values.json)
- Live LigaInsider Scraping & KickTipp-Spieltagsprognosen
"""

import json
import os
import streamlit as st
import pandas as pd
from data_collector import run_pipeline, KickbaseAPIError, LigaInsiderError
from optimizer import (
    optimize_lineup,
    evaluate_manual_lineup,
    get_valid_formations,
    OptimizationError,
)
from kicktipp_engine import run_kicktipp_live_pipeline

# ---------------------------------------------------------------------------
# Page Config & Account-Bound Persistence Helpers
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="⚽ Kickbase & KickTipp Optimizer",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_MV_FILE = "custom_market_values.json"


def _normalize_user_key(email: str) -> str:
    return (email or "").strip().lower()


def _load_mv_store() -> dict:
    """
    Lädt den JSON-Speicher für manuelle Marktwerte.
    Unterstützt automatische Migration des alten flachen Formats auf Account-Bindung.
    """
    if os.path.exists(CUSTOM_MV_FILE):
        try:
            with open(CUSTOM_MV_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                if "accounts" in raw and isinstance(raw["accounts"], dict):
                    raw.setdefault("_legacy", {})
                    raw.setdefault("last_email", "")
                    return raw
                # Altes flaches Format: {"Spielername": 2800000.0, ...}
                legacy_mvs = {
                    str(k): float(v)
                    for k, v in raw.items()
                    if isinstance(v, (int, float))
                }
                return {"accounts": {}, "_legacy": legacy_mvs, "last_email": ""}
        except Exception:
            pass
    return {"accounts": {}, "_legacy": {}, "last_email": ""}


def _write_mv_store(store: dict):
    with open(CUSTOM_MV_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=4, ensure_ascii=False)


def get_last_saved_email() -> str:
    store = _load_mv_store()
    return store.get("last_email", "") or os.getenv("KICKBASE_EMAIL", "")


def load_custom_market_values(email: str = "") -> dict:
    """
    Lädt die gespeicherten Marktwerte für den angegebenen Kickbase-Account (E-Mail).
    Migriert vorhandene Legacy-Werte automatisch beim ersten Eingeben der Zugangsdaten.
    """
    store = _load_mv_store()
    user_key = _normalize_user_key(email)

    if not user_key:
        # Falls noch keine E-Mail eingegeben wurde, zeige ggf. letztes Konto oder Legacy-Werte
        last_key = _normalize_user_key(store.get("last_email", ""))
        if last_key and last_key in store["accounts"]:
            return dict(store["accounts"][last_key])
        return dict(store.get("_legacy", {}))

    changed = False
    if user_key not in store["accounts"]:
        if store.get("_legacy"):
            # Bestehende Werte einmalig an diesen Account binden
            store["accounts"][user_key] = dict(store["_legacy"])
            store["_legacy"] = {}
            changed = True
        else:
            store["accounts"][user_key] = {}
            changed = True

    if store.get("last_email") != email.strip():
        store["last_email"] = email.strip()
        changed = True

    if changed:
        _write_mv_store(store)

    return dict(store["accounts"].get(user_key, {}))


def save_custom_market_values(data: dict, email: str = ""):
    """
    Speichert manuelle Marktwerte dauerhaft und fest an die Kickbase-Zugangsdaten (E-Mail) gebunden.
    """
    store = _load_mv_store()
    user_key = _normalize_user_key(email) or _normalize_user_key(store.get("last_email", ""))

    clean_data = {str(k): float(v) for k, v in data.items()}
    if user_key:
        store["accounts"][user_key] = clean_data
        if email and email.strip():
            store["last_email"] = email.strip()
        if store.get("_legacy"):
            store["_legacy"] = {}
    else:
        store["_legacy"] = clean_data

    _write_mv_store(store)


def apply_custom_market_values(df: pd.DataFrame, custom_mvs: dict) -> pd.DataFrame:
    """
    Setzt Marktwerte zunächst auf die Original-Arena-Werte (base_market_value) zurück
    und wendet anschließend die account-gebundenen manuellen Marktwerte an.
    """
    if df is None or df.empty:
        return df

    df = df.copy()
    if "base_market_value" in df.columns:
        df["market_value"] = df["base_market_value"].astype(float)
    else:
        df["base_market_value"] = df["market_value"].astype(float)

    if not custom_mvs:
        return df

    for pid_or_name, val in custom_mvs.items():
        key_str = str(pid_or_name).strip()
        mask_id = df["id"].astype(str).str.strip() == key_str
        if mask_id.any():
            df.loc[mask_id, "market_value"] = float(val)
        else:
            mask_name = df["name"].astype(str).str.strip().str.lower() == key_str.lower()
            if mask_name.any():
                df.loc[mask_name, "market_value"] = float(val)
    return df


def refresh_dataframes_with_custom_mvs():
    """Aktualisiert all_players, optimizable und ggf. das aktuelle Resultat mit den Account-Marktwerten."""
    custom_mvs = st.session_state.get("custom_mvs", {})
    if st.session_state.get("all_players") is not None:
        st.session_state["all_players"] = apply_custom_market_values(
            st.session_state["all_players"], custom_mvs
        )
    if st.session_state.get("optimizable") is not None:
        st.session_state["optimizable"] = apply_custom_market_values(
            st.session_state["optimizable"], custom_mvs
        )


def get_solver_dataframe(included_ids: list[str] | None = None) -> pd.DataFrame:
    """
    Liefert das DataFrame für den Optimizer. Stellt sicher, dass manuell gehaltene
    Spieler aus all_players auch dann enthalten sind, wenn sie z. B. 0 Ø-Punkte haben.
    """
    opt_df = st.session_state["optimizable"].copy()
    opt_df["id"] = opt_df["id"].astype(str).str.strip()

    if included_ids and st.session_state.get("all_players") is not None:
        all_df = st.session_state["all_players"].copy()
        all_df["id"] = all_df["id"].astype(str).str.strip()
        missing_ids = {str(pid).strip() for pid in included_ids} - set(opt_df["id"])
        if missing_ids:
            extra_rows = all_df[all_df["id"].isin(missing_ids)].copy()
            if not extra_rows.empty:
                if "p_start" not in extra_rows.columns:
                    extra_rows["p_start"] = 1.0
                if "xp" not in extra_rows.columns:
                    extra_rows["xp"] = extra_rows["average_points"] * extra_rows["p_start"]
                opt_df = pd.concat([opt_df, extra_rows], ignore_index=True)

    return opt_df


# ---------------------------------------------------------------------------
# State Initialization
# ---------------------------------------------------------------------------
if "kb_email" not in st.session_state:
    st.session_state["kb_email"] = get_last_saved_email()
if "kb_password" not in st.session_state:
    st.session_state["kb_password"] = os.getenv("KICKBASE_PASSWORD", "")
if "result" not in st.session_state:
    st.session_state["result"] = None
if "all_players" not in st.session_state:
    st.session_state["all_players"] = None
if "optimizable" not in st.session_state:
    st.session_state["optimizable"] = None
if "locked_out_players" not in st.session_state:
    st.session_state["locked_out_players"] = {}
if "pinned_players" not in st.session_state:
    st.session_state["pinned_players"] = {}
if "active_mv_account" not in st.session_state:
    st.session_state["active_mv_account"] = _normalize_user_key(st.session_state["kb_email"])
if "custom_mvs" not in st.session_state:
    st.session_state["custom_mvs"] = load_custom_market_values(st.session_state["kb_email"])
if "kicktipp_results" not in st.session_state:
    st.session_state["kicktipp_results"] = None

# ---------------------------------------------------------------------------
# Responsive CSS (Desktop + Mobile)
# ---------------------------------------------------------------------------
st.markdown("""
<style>
    .main .block-container { padding-top: 1.5rem; max-width: 1200px; }
    .pitch-container {
        background: linear-gradient(180deg, #1b5e20 0%, #2e7d32 30%, #388e3c 60%, #43a047 100%);
        border-radius: 16px; padding: 1.5rem 1rem; margin: 1rem 0; position: relative;
        min-height: 480px; border: 3px solid rgba(255,255,255,0.15);
        box-shadow: 0 8px 32px rgba(0,0,0,0.3);
    }
    .player-card {
        background: rgba(255,255,255,0.95); border-radius: 10px; padding: 0.5rem 0.6rem;
        text-align: center; box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        transition: transform 0.2s ease; min-width: 0;
    }
    .player-card.pinned-card {
        border: 2px solid #1976d2;
        background: #f0f7ff;
    }
    .player-card:hover { transform: translateY(-3px); box-shadow: 0 6px 20px rgba(0,0,0,0.3); }
    .player-card .name {
        font-weight: 700; font-size: 0.8rem; color: #1a1a1a; margin-bottom: 2px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .player-card .team { font-size: 0.65rem; color: #666; margin-bottom: 3px; }
    .player-card .xp { font-size: 0.75rem; color: #1b5e20; font-weight: 700; }
    .player-card .mv { font-size: 0.65rem; color: #888; }
    .captain-badge {
        display: inline-block; background: linear-gradient(135deg, #ffd700, #ffb300);
        color: #1a1a1a; font-weight: 800; font-size: 0.6rem; padding: 1px 5px;
        border-radius: 6px; margin-left: 3px;
    }
    .pin-badge {
        display: inline-block; background: #1976d2;
        color: #ffffff; font-weight: 700; font-size: 0.58rem; padding: 1px 4px;
        border-radius: 5px; margin-left: 3px;
    }
    .kpi-card {
        background: linear-gradient(135deg, #f8f9fa, #e9ecef); border-radius: 12px;
        padding: 1rem; text-align: center; border: 1px solid #dee2e6;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }
    .kpi-card .value { font-size: 1.6rem; font-weight: 800; color: #1b5e20; }
    .kpi-card .label { font-size: 0.75rem; color: #6c757d; text-transform: uppercase; letter-spacing: 1px; }
    .section-header {
        font-size: 1.1rem; font-weight: 700; color: #1a1a1a;
        margin: 1.2rem 0 0.6rem 0; padding-bottom: 0.3rem; border-bottom: 2px solid #1b5e20;
    }
    
    /* Mobile Media Queries */
    @media (max-width: 768px) {
        .main .block-container { padding: 0.8rem 0.3rem; }
        .player-card { padding: 0.3rem 0.2rem; }
        .player-card .name { font-size: 0.65rem; }
        .player-card .xp, .player-card .mv { font-size: 0.55rem; }
        .kpi-card .value { font-size: 1.2rem; }
        .kpi-card .label { font-size: 0.65rem; }
    }
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------
def format_market_value(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} Mio. €"
    elif value >= 1_000:
        return f"{value / 1_000:.0f} Tsd. €"
    return f"{value:.0f} €"


def render_player_card(player: pd.Series, is_captain: bool = False, is_pinned: bool = False) -> str:
    captain_html = '<span class="captain-badge">C</span>' if is_captain else ""
    pin_html = '<span class="pin-badge" title="Gehaltener Spieler">📌</span>' if is_pinned else ""
    extra_cls = " pinned-card" if is_pinned else ""
    return f"""
    <div class="player-card{extra_cls}">
        <div class="name">{player['name']}{captain_html}{pin_html}</div>
        <div class="team">{player.get('team', '')}</div>
        <div class="xp">⚡ {player['xp']:.1f} xP</div>
        <div class="mv">{format_market_value(player['market_value'])}</div>
    </div>
    """


def render_pitch(lineup: pd.DataFrame, captain_id: str):
    st.markdown('<div class="section-header">⚽ Aktuelle Aufstellung</div>', unsafe_allow_html=True)
    positions = {
        1: lineup[lineup["position"] == 1],
        2: lineup[lineup["position"] == 2],
        3: lineup[lineup["position"] == 3],
        4: lineup[lineup["position"] == 4],
    }
    position_labels = {4: "🔴 STURM", 3: "🟡 MITTELFELD", 2: "🔵 ABWEHR", 1: "🟢 TORWART"}
    pinned_ids = set(st.session_state.get("pinned_players", {}).keys())

    for pos in [4, 3, 2, 1]:
        pos_players = positions.get(pos, pd.DataFrame())
        if pos_players.empty:
            continue
        st.markdown(
            f"<div style='text-align:center;color:#888;font-size:0.75rem;font-weight:600;letter-spacing:2px;margin:0.5rem 0 0.3rem;'>"
            f"{position_labels[pos]}</div>",
            unsafe_allow_html=True,
        )
        n_players = len(pos_players)
        cols = st.columns(n_players, gap="small")
        for idx, (_, player) in enumerate(pos_players.iterrows()):
            pid_str = str(player.get("id", "")).strip()
            is_cap = pid_str == str(captain_id).strip() or bool(player.get("is_captain", False))
            is_pin = pid_str in pinned_ids or bool(player.get("is_pinned", False))
            with cols[idx]:
                st.markdown(render_player_card(player, is_cap, is_pin), unsafe_allow_html=True)


def render_kpis(result: dict):
    st.markdown('<div class="section-header">📊 KPI-Übersicht</div>', unsafe_allow_html=True)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(f'<div class="kpi-card"><div class="value">⚡ {result["total_xp"]:.1f}</div><div class="label">Erwartete Punkte (inkl. C)</div></div>', unsafe_allow_html=True)
    with col2:
        st.markdown(f'<div class="kpi-card"><div class="value">💰 {format_market_value(result["total_market_value"])}</div><div class="label">Genutztes Budget (Arena)</div></div>', unsafe_allow_html=True)
    with col3:
        st.markdown(f'<div class="kpi-card"><div class="value">🏦 {format_market_value(result["budget_remaining"])}</div><div class="label">Rest-Budget</div></div>', unsafe_allow_html=True)
    with col4:
        st.markdown(f'<div class="kpi-card"><div class="value">📐 {result["formation"]}</div><div class="label">Formation</div></div>', unsafe_allow_html=True)


def render_detail_table(lineup: pd.DataFrame, captain_id: str):
    st.markdown('<div class="section-header">📋 Detaillierte Aufstellung</div>', unsafe_allow_html=True)
    display_df = lineup.copy()
    pinned_ids = set(st.session_state.get("pinned_players", {}).keys())

    display_df["Status"] = display_df.apply(
        lambda r: " ".join(
            filter(
                None,
                [
                    "👑 C" if (str(r.get("id", "")) == str(captain_id) or r.get("is_captain", False)) else "",
                    "📌 Gehalten" if (str(r.get("id", "")) in pinned_ids or r.get("is_pinned", False)) else "",
                ],
            )
        ),
        axis=1,
    )
    display_df["Marktwert (Arena)"] = display_df["market_value"].apply(format_market_value)
    display_df["Position"] = display_df["position_label"] if "position_label" in display_df.columns else display_df["position"]
    display_df["xP"] = display_df["xp"].round(1)
    display_df["Ø Punkte"] = display_df["average_points"].round(1)
    display_df["P(Start)"] = display_df["p_start"].apply(lambda p: f"{p:.0%}")
    display_df["LigaInsider"] = display_df.get("li_status", "—")

    columns_to_show = ["Status", "Name", "Verein", "Position", "Marktwert (Arena)", "Ø Punkte", "P(Start)", "xP", "LigaInsider"]
    display_df = display_df.rename(columns={"name": "Name", "team": "Verein"})
    existing_cols = [c for c in columns_to_show if c in display_df.columns]
    st.dataframe(display_df[existing_cols], use_container_width=True, hide_index=True, height=430)


# ---------------------------------------------------------------------------
# Sidebar (Lineup & Account-Gebundene Persistenz)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("# ⚽ Kickbase & KickTipp")
    st.markdown("### Manager Dashboard")
    st.markdown("---")

    st.markdown("#### 🔑 Kickbase Zugangsdaten")
    email = st.text_input(
        "Kickbase E-Mail",
        value=st.session_state.get("kb_email", ""),
        placeholder="deine@email.de",
        help="Deine gespeicherten Marktwerte werden automatisch an diese E-Mail-Adresse gebunden.",
    )
    password = st.text_input(
        "Kickbase Passwort",
        value=st.session_state.get("kb_password", ""),
        type="password",
        placeholder="Dein Passwort",
    )

    # Sobald eine E-Mail eingegeben oder geändert wird: zugehörige Marktwerte laden & binden
    normalized_input_email = _normalize_user_key(email)
    if normalized_input_email and normalized_input_email != st.session_state.get("active_mv_account", ""):
        st.session_state["kb_email"] = email.strip()
        st.session_state["active_mv_account"] = normalized_input_email
        st.session_state["custom_mvs"] = load_custom_market_values(email.strip())
        refresh_dataframes_with_custom_mvs()

    active_email_for_mv = email.strip() if email and email.strip() else st.session_state.get("kb_email", "")

    st.markdown("---")
    st.markdown("#### ⚙️ Arena-Einstellungen")

    budget = st.slider("💰 Budget (Mio. €)", min_value=50, max_value=300, value=150, step=5)
    budget_value = budget * 1_000_000

    formations = get_valid_formations()
    formation_options = ["auto (beste finden)"] + list(formations.keys())
    selected_formation = st.selectbox("📐 Formation", options=formation_options, index=0)
    formation_key = "auto" if selected_formation.startswith("auto") else selected_formation

    st.markdown("---")
    sync_clicked = st.button("🔄 Daten neu laden & Optimieren", type="primary", use_container_width=True)

    # Gehaltene (Fixierte) Spieler Übersicht
    if st.session_state["pinned_players"]:
        st.markdown("---")
        st.markdown(f"#### 📌 Gehaltene Spieler ({len(st.session_state['pinned_players'])})")
        for pid, pname in list(st.session_state["pinned_players"].items()):
            c1, c2 = st.columns([3, 1])
            c1.caption(f"📌 {pname}")
            if c2.button("Lösen", key=f"unpin_side_{pid}"):
                del st.session_state["pinned_players"][pid]
                st.rerun()

        if st.button("🔓 Alle gehaltenen Spieler lösen", use_container_width=True):
            st.session_state["pinned_players"] = {}
            st.rerun()

    # Gesperrte Spieler Übersicht
    if st.session_state["locked_out_players"]:
        st.markdown("---")
        st.markdown(f"#### 🚫 Gesperrte Spieler ({len(st.session_state['locked_out_players'])})")
        for pid, pname in list(st.session_state["locked_out_players"].items()):
            c1, c2 = st.columns([3, 1])
            c1.caption(f"❌ {pname}")
            if c2.button("Freigeben", key=f"unlock_side_{pid}"):
                del st.session_state["locked_out_players"][pid]
                st.rerun()

        if st.button("🗑️ Alle Sperren löschen", use_container_width=True):
            st.session_state["locked_out_players"] = {}
            st.rerun()

    # Gespeicherte manuelle Marktwerte (Account-gebunden)
    st.markdown("---")
    st.markdown("#### 💰 Gespeicherte Marktwerte")
    if active_email_for_mv:
        st.caption(f"🔗 Gebunden an: **{active_email_for_mv}**")
    else:
        st.caption("ℹ️ Gib oben deine Kickbase E-Mail ein, um Marktwerte an deinen Account zu binden.")

    if st.session_state["custom_mvs"]:
        for p_key, p_val in list(st.session_state["custom_mvs"].items()):
            c1, c2 = st.columns([3, 1])
            c1.caption(f"{p_key}: **{format_market_value(p_val)}**")
            if c2.button("❌", key=f"del_mv_{p_key}", help="Korrektur löschen"):
                del st.session_state["custom_mvs"][p_key]
                save_custom_market_values(st.session_state["custom_mvs"], active_email_for_mv)
                refresh_dataframes_with_custom_mvs()
                st.rerun()

        if st.button("🗑️ Alle Korrekturen löschen", use_container_width=True):
            st.session_state["custom_mvs"] = {}
            save_custom_market_values({}, active_email_for_mv)
            refresh_dataframes_with_custom_mvs()
            st.rerun()
    else:
        st.caption("Keine manuellen Marktwerte für diesen Account hinterlegt.")


# ---------------------------------------------------------------------------
# Hauptbereich mit Tabs
# ---------------------------------------------------------------------------
st.markdown(
    "<h1 style='text-align:center;margin-bottom:0;'>⚽ Kickbase Arena & KickTipp</h1>"
    "<p style='text-align:center;color:#666;margin-top:0.2rem;'>"
    "Optimale Arena-Aufstellungen, manueller Team-Builder & datenbasierte Spieltagsprognosen</p>",
    unsafe_allow_html=True,
)

tab_optimizer, tab_kicktipp = st.tabs(["🧮 Lineup Optimizer & Team-Builder", "🎯 KickTipp Prognosen"])


# ===========================================================================
# TAB 1: KICKBASE LINEUP OPTIMIZER & TEAM-BUILDER
# ===========================================================================
with tab_optimizer:
    # 1. Pipeline ausführen wenn Login geklickt
    if sync_clicked:
        if not email or not password:
            st.error("⚠️ Bitte E-Mail und Passwort in der Sidebar eingeben.")
        else:
            st.session_state["kb_email"] = email.strip()
            st.session_state["kb_password"] = password
            st.session_state["active_mv_account"] = _normalize_user_key(email)
            st.session_state["custom_mvs"] = load_custom_market_values(email.strip())

            with st.spinner("🔄 Kickbase-Login, Arena-Marktwerte & LigaInsider-Abruf..."):
                try:
                    all_players, optimizable = run_pipeline(email.strip(), password)
                    all_players = apply_custom_market_values(all_players, st.session_state["custom_mvs"])
                    optimizable = apply_custom_market_values(optimizable, st.session_state["custom_mvs"])
                    st.session_state["all_players"] = all_players
                    st.session_state["optimizable"] = optimizable
                except KickbaseAPIError as e:
                    st.error(f"❌ Kickbase-Fehler: {e}")
                    st.stop()
                except Exception as e:
                    st.error(f"❌ Unerwarteter Fehler: {e}")
                    st.stop()

            with st.spinner("🧮 Optimierung läuft..."):
                try:
                    pinned_ids = list(st.session_state["pinned_players"].keys())
                    solver_df = get_solver_dataframe(pinned_ids)
                    result = optimize_lineup(
                        solver_df,
                        budget=budget_value,
                        formation=formation_key,
                        excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                        included_player_ids=pinned_ids,
                    )
                    st.session_state["result"] = result
                except OptimizationError as e:
                    st.error(f"❌ {e}")
                    st.stop()

    # 2. Wenn Daten geladen sind: Werkzeuge für Marktwert, Spieler halten/sperren & Team selbst aufstellen
    if st.session_state.get("optimizable") is not None and st.session_state.get("result") is not None:
        current_lineup = st.session_state["result"]["lineup"]
        full_pool_df = (
            st.session_state["all_players"]
            if st.session_state.get("all_players") is not None
            else st.session_state["optimizable"]
        )

        # --- BEREICH A: MARKTWERT MANUELL ANPASSEN (ACCOUNT-GEBUNDEN) ---
        with st.expander("✏️ Arena-Marktwert eines Spielers manuell anpassen & dauerhaft für deinen Account speichern", expanded=False):
            player_names = sorted(full_pool_df["name"].dropna().unique().tolist())

            edit_col1, edit_col2, edit_col3 = st.columns([2, 2, 1])
            with edit_col1:
                selected_player = st.selectbox(
                    "Spieler wählen:",
                    options=player_names,
                    index=0 if player_names else None,
                )

            current_val = 0.0
            if selected_player:
                match_row = full_pool_df[full_pool_df["name"] == selected_player]
                if not match_row.empty:
                    current_val = float(match_row.iloc[0]["market_value"])

            with edit_col2:
                new_mv_input = st.number_input(
                    "Neuer Arena-Marktwert in € (z. B. 2800000):",
                    value=int(current_val),
                    step=100_000,
                    format="%d",
                )

            with edit_col3:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("💾 Speichern & Anwenden", type="primary", use_container_width=True):
                    if selected_player and new_mv_input > 0:
                        st.session_state["custom_mvs"][selected_player] = float(new_mv_input)
                        save_custom_market_values(st.session_state["custom_mvs"], active_email_for_mv)
                        refresh_dataframes_with_custom_mvs()

                        try:
                            pinned_ids = list(st.session_state["pinned_players"].keys())
                            solver_df = get_solver_dataframe(pinned_ids)
                            new_result = optimize_lineup(
                                solver_df,
                                budget=budget_value,
                                formation=formation_key,
                                excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                                included_player_ids=pinned_ids,
                            )
                            st.session_state["result"] = new_result
                            st.success(
                                f"Marktwert für {selected_player} auf {format_market_value(new_mv_input)} "
                                f"gespeichert (Account: {active_email_for_mv or 'Lokal'})!"
                            )
                            st.rerun()
                        except OptimizationError as e:
                            st.error(f"❌ {e}")

        # --- BEREICH B: TEAM SELBST AUFSTELLEN / WUNSCHSPIELER KOMBINIEREN ---
        with st.expander("🛠️ Team selbst aufstellen / Beliebige Wunschspieler vorgeben", expanded=False):
            st.caption(
                "Stelle dir hier dein eigenes Team zusammen (komplette 11 Spieler) oder wähle einzelne Wunschspieler aus "
                "und lass den Optimizer die restlichen freien Positionen optimal auffüllen."
            )

            tb_top1, tb_top2, tb_top3 = st.columns([2, 2, 2])
            with tb_top1:
                only_fit_in_builder = st.checkbox("Nur fitte Spieler im Builder anzeigen", value=True)
            with tb_top2:
                load_current_clicked = st.button("📥 Aktuelle Elf in den Builder laden", use_container_width=True)
            with tb_top3:
                clear_builder_clicked = st.button("🗑️ Builder-Auswahl leeren", use_container_width=True)

            builder_pool = full_pool_df.copy()
            if "xp" not in builder_pool.columns:
                builder_pool["xp"] = builder_pool.get("average_points", 0.0) * builder_pool.get("p_start", 1.0)
            if only_fit_in_builder and "status" in builder_pool.columns:
                # Behalte bereits gepinnte oder in der aktuellen Elf stehende Spieler trotzdem in der Auswahl
                keep_ids = set(current_lineup["id"].astype(str)) | set(st.session_state["pinned_players"].keys())
                builder_pool = builder_pool[
                    (builder_pool["status"] == "fit") | (builder_pool["id"].astype(str).isin(keep_ids))
                ].copy()

            builder_pool = builder_pool.sort_values(["xp", "market_value"], ascending=[False, False]).reset_index(drop=True)

            # Option-Labels pro Position aufbauen
            pos_options_map: dict[int, dict[str, str]] = {1: {}, 2: {}, 3: {}, 4: {}}
            id_to_label_map: dict[str, str] = {}
            for _, row in builder_pool.iterrows():
                pid = str(row["id"]).strip()
                pos = int(row.get("position", 3))
                label = (
                    f"{row['name']} ({row.get('team', '')} | "
                    f"{format_market_value(row['market_value'])} | {row['xp']:.1f} xP)"
                )
                if pos in pos_options_map:
                    pos_options_map[pos][label] = pid
                    id_to_label_map[pid] = label

            # Session-State für die 4 Builder-Multiselects initialisieren/steuern
            for pos in (1, 2, 3, 4):
                ms_key = f"builder_pos_{pos}"
                if ms_key not in st.session_state:
                    # Standardmäßig mit bereits gehaltenen Spielern vorbelegen
                    default_labels = [
                        id_to_label_map[pid]
                        for pid in st.session_state["pinned_players"].keys()
                        if pid in id_to_label_map and id_to_label_map[pid] in pos_options_map[pos]
                    ]
                    st.session_state[ms_key] = default_labels

            if load_current_clicked:
                for pos in (1, 2, 3, 4):
                    pos_ids = current_lineup[current_lineup["position"] == pos]["id"].astype(str).str.strip().tolist()
                    st.session_state[f"builder_pos_{pos}"] = [
                        id_to_label_map[pid] for pid in pos_ids if pid in id_to_label_map and id_to_label_map[pid] in pos_options_map[pos]
                    ]
                st.rerun()

            if clear_builder_clicked:
                for pos in (1, 2, 3, 4):
                    st.session_state[f"builder_pos_{pos}"] = []
                st.rerun()

            b_col1, b_col2 = st.columns(2)
            with b_col1:
                sel_tw = st.multiselect(
                    "🟢 Torwart (1 für Vollaufstellung):",
                    options=list(pos_options_map[1].keys()),
                    key="builder_pos_1",
                    placeholder="Torwart wählen...",
                )
                sel_mit = st.multiselect(
                    "🟡 Mittelfeld (3–5 für Vollaufstellung):",
                    options=list(pos_options_map[3].keys()),
                    key="builder_pos_3",
                    placeholder="Mittelfeldspieler wählen...",
                )
            with b_col2:
                sel_abw = st.multiselect(
                    "🔵 Abwehr (3–5 für Vollaufstellung):",
                    options=list(pos_options_map[2].keys()),
                    key="builder_pos_2",
                    placeholder="Abwehrspieler wählen...",
                )
                sel_st = st.multiselect(
                    "🔴 Sturm (1–3 für Vollaufstellung):",
                    options=list(pos_options_map[4].keys()),
                    key="builder_pos_4",
                    placeholder="Stürmer wählen...",
                )

            builder_selected_ids = (
                [pos_options_map[1][lbl] for lbl in sel_tw if lbl in pos_options_map[1]]
                + [pos_options_map[2][lbl] for lbl in sel_abw if lbl in pos_options_map[2]]
                + [pos_options_map[3][lbl] for lbl in sel_mit if lbl in pos_options_map[3]]
                + [pos_options_map[4][lbl] for lbl in sel_st if lbl in pos_options_map[4]]
            )

            builder_df = builder_pool[builder_pool["id"].astype(str).isin(set(builder_selected_ids))].copy()
            b_count = len(builder_df)
            b_mv = float(builder_df["market_value"].sum()) if b_count > 0 else 0.0
            b_rem = budget_value - b_mv
            b_xp = float(builder_df["xp"].sum()) if b_count > 0 else 0.0

            st.markdown(
                f"**Auswahl-Status:** `{b_count} / 11 Spieler` "
                f"(`{len(sel_tw)} TW - {len(sel_abw)} ABW - {len(sel_mit)} MIT - {len(sel_st)} ST`) &nbsp;|&nbsp; "
                f"**Kosten:** `{format_market_value(b_mv)}` &nbsp;|&nbsp; "
                f"**Restbudget:** `{format_market_value(b_rem)}` &nbsp;|&nbsp; "
                f"**Basis-xP:** `{b_xp:.1f}`"
            )

            # Optionale Kapitänswahl bei 11 Spielern
            manual_cap_id = None
            if b_count == 11:
                cap_options = {"⚡ Automatisch (Spieler mit höchsten xP)": ""}
                for _, r in builder_df.iterrows():
                    cap_options[f"👑 {r['name']} ({r['xp']:.1f} xP)"] = str(r["id"])
                chosen_cap_label = st.selectbox("Kapitän für deine manuelle Aufstellung:", options=list(cap_options.keys()))
                manual_cap_id = cap_options[chosen_cap_label] or None

            act_col1, act_col2 = st.columns(2)
            with act_col1:
                if st.button(
                    "🧮 Gewählte Spieler halten & restliche Plätze optimal suchen",
                    type="primary",
                    use_container_width=True,
                    disabled=(b_count == 0 or b_count > 11),
                ):
                    st.session_state["pinned_players"] = {
                        str(r["id"]).strip(): r["name"] for _, r in builder_df.iterrows()
                    }
                    # Sicherstellen, dass gehaltene Spieler nicht gleichzeitig gesperrt sind
                    for pid in st.session_state["pinned_players"]:
                        st.session_state["locked_out_players"].pop(pid, None)

                    try:
                        pinned_ids = list(st.session_state["pinned_players"].keys())
                        solver_df = get_solver_dataframe(pinned_ids)
                        new_result = optimize_lineup(
                            solver_df,
                            budget=budget_value,
                            formation=formation_key,
                            excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                            included_player_ids=pinned_ids,
                        )
                        st.session_state["result"] = new_result
                        st.rerun()
                    except OptimizationError as e:
                        st.error(f"❌ {e}")

            with act_col2:
                if st.button(
                    "⚽ Komplette eigene Elf (11 Spieler) direkt übernehmen",
                    use_container_width=True,
                    disabled=(b_count != 11),
                ):
                    try:
                        manual_res = evaluate_manual_lineup(
                            full_pool_df,
                            selected_player_ids=builder_selected_ids,
                            budget=budget_value,
                            captain_id=manual_cap_id,
                            pinned_player_ids=builder_selected_ids,
                        )
                        st.session_state["pinned_players"] = {
                            str(r["id"]).strip(): r["name"] for _, r in builder_df.iterrows()
                        }
                        st.session_state["result"] = manual_res
                        st.rerun()
                    except OptimizationError as e:
                        st.error(f"❌ {e}")

        # --- BEREICH C: EINZELNE SPIELER HALTEN & REST NEU SUCHEN / SPERREN ---
        st.markdown("---")
        hold_col, block_col = st.columns(2)

        player_choices = {
            f"{row['name']} ({row.get('team', '')} - {row['position_label']})": str(row["id"]).strip()
            for _, row in current_lineup.iterrows()
        }
        id_to_choice_label = {v: k for k, v in player_choices.items()}

        with hold_col:
            st.markdown("### 📌 Spieler halten & Rest neu suchen")
            default_held_labels = [
                id_to_choice_label[pid]
                for pid in st.session_state["pinned_players"].keys()
                if pid in id_to_choice_label
            ]
            selected_to_hold = st.multiselect(
                "Wähle Spieler aus der aktuellen Elf, die du **behalten (halten)** willst:",
                options=list(player_choices.keys()),
                default=default_held_labels,
                placeholder="Spieler zum Halten auswählen...",
            )
            replace_unheld = st.checkbox(
                "Übrige (nicht gehaltene) Spieler dieser Aufstellung ersetzen (komplett neu suchen)",
                value=True,
                help="Wenn aktiv, werden die nicht gehaltenen Spieler der aktuellen Elf gesperrt, damit der Solver für die offenen Plätze garantiert neue Spieler sucht.",
            )

            if st.button("📌 Gehaltene Spieler fixieren & Rest neu suchen", type="primary", use_container_width=True):
                held_ids = {player_choices[lbl]: lbl.split(" (")[0] for lbl in selected_to_hold}
                st.session_state["pinned_players"] = held_ids

                # Gehaltene Spieler aus evtl. Sperrliste entfernen
                for pid in held_ids:
                    st.session_state["locked_out_players"].pop(pid, None)

                if replace_unheld:
                    for lbl, pid in player_choices.items():
                        if pid not in held_ids:
                            st.session_state["locked_out_players"][pid] = lbl.split(" (")[0]

                try:
                    pinned_ids = list(st.session_state["pinned_players"].keys())
                    solver_df = get_solver_dataframe(pinned_ids)
                    new_result = optimize_lineup(
                        solver_df,
                        budget=budget_value,
                        formation=formation_key,
                        excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                        included_player_ids=pinned_ids,
                    )
                    st.session_state["result"] = new_result
                    st.rerun()
                except OptimizationError as e:
                    st.error(f"❌ {e}")

        with block_col:
            st.markdown("### 🚫 Einzelne Spieler sperren")
            selected_to_block = st.multiselect(
                "Wähle Spieler aus der aktuellen Elf, die du **ausschließen (sperren)** willst:",
                options=list(player_choices.keys()),
                placeholder="Spieler zum Sperren auswählen...",
            )
            keep_others_when_blocking = st.checkbox(
                "Übrige (nicht gesperrte) Spieler der aktuellen Elf dabei behalten",
                value=False,
                help="Wenn aktiv, bleiben alle anderen Spieler deiner aktuellen Aufstellung erhalten und es wird nur für die gesperrten Spieler Ersatz gesucht.",
            )

            b_btn1, b_btn2 = st.columns([2, 1])
            with b_btn1:
                if st.button("🚫 Sperren & Neu berechnen", type="primary", use_container_width=True):
                    if selected_to_block:
                        blocked_now_ids = set()
                        for label in selected_to_block:
                            p_id = player_choices[label]
                            p_name = label.split(" (")[0]
                            st.session_state["locked_out_players"][p_id] = p_name
                            st.session_state["pinned_players"].pop(p_id, None)
                            blocked_now_ids.add(p_id)

                        if keep_others_when_blocking:
                            for label, p_id in player_choices.items():
                                if p_id not in blocked_now_ids:
                                    st.session_state["pinned_players"][p_id] = label.split(" (")[0]

                        try:
                            pinned_ids = list(st.session_state["pinned_players"].keys())
                            solver_df = get_solver_dataframe(pinned_ids)
                            new_result = optimize_lineup(
                                solver_df,
                                budget=budget_value,
                                formation=formation_key,
                                excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                                included_player_ids=pinned_ids,
                            )
                            st.session_state["result"] = new_result
                            st.rerun()
                        except OptimizationError as e:
                            st.error(f"❌ {e}")

            with b_btn2:
                if st.button("🔄 Neu optimieren", use_container_width=True):
                    try:
                        pinned_ids = list(st.session_state["pinned_players"].keys())
                        solver_df = get_solver_dataframe(pinned_ids)
                        new_result = optimize_lineup(
                            solver_df,
                            budget=budget_value,
                            formation=formation_key,
                            excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                            included_player_ids=pinned_ids,
                        )
                        st.session_state["result"] = new_result
                        st.rerun()
                    except OptimizationError as e:
                        st.error(f"❌ {e}")

    # 3. Anzeige des Spielfelds & KPIs
    result = st.session_state.get("result")
    if result:
        render_kpis(result)

        if "all_formation_scores" in result and result["all_formation_scores"]:
            with st.expander("🔍 Mathematischer Formations-Vergleich (xP aller Formationen)", expanded=False):
                scores_df = pd.DataFrame(
                    [
                        {"Formation": f, "Erreichbare xP": pts, "Gewählt": "✅ Ja" if f == result["formation"] else "Nein"}
                        for f, pts in sorted(result["all_formation_scores"].items(), key=lambda x: x[1], reverse=True)
                    ]
                )
                st.dataframe(scores_df, use_container_width=True, hide_index=True)

        st.markdown("")
        render_pitch(result["lineup"], result["captain_id"])
        st.markdown("")
        render_detail_table(result["lineup"], result["captain_id"])
    else:
        st.markdown("---")
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            st.markdown(
                """
                <div style="text-align:center; padding:3rem 2rem; background: linear-gradient(135deg, #f8f9fa, #e9ecef);
                            border-radius:16px; border:1px solid #dee2e6;">
                    <div style="font-size:3rem; margin-bottom:1rem;">🏟️</div>
                    <h3 style="color:#1a1a1a; margin:0 0 0.5rem;">Bereit für den Spieltag?</h3>
                    <p style="color:#666; margin:0; font-size:0.9rem;">
                        Gib deine Zugangsdaten ein und klicke auf <strong>„Daten neu laden & Optimieren"</strong>.
                    </p>
                </div>
                """,
                unsafe_allow_html=True,
            )


# ===========================================================================
# TAB 2: KICKTIPP LIVE-PROGNOSEN
# ===========================================================================
with tab_kicktipp:
    st.markdown("### 🎯 KickTipp Live-Analyse & Spieltagstipps")
    st.caption(
        "Berücksichtigt alle bisherigen Saisonergebnisse, die aktuelle Bundesliga-Tabelle, "
        "Tore/Gegentore (inkl. Heim-/Auswärtsbilanz), fehlende Schlüsselspieler sowie taktische "
        "LigaInsider-Daten (Standards-Stärke vs. Standards-Anfälligkeit, Luftkampf, Großchancen, Fehler vor Gegentor)."
    )

    btn_scrape_kt = st.button("🔄 LigaInsider & OpenLigaDB jetzt live analysieren & Tipps berechnen", type="primary")

    if btn_scrape_kt or st.session_state["kicktipp_results"] is None:
        with st.spinner("🕵️ Lade Saisonergebnisse, aktuelle Tabelle, Schlüsselspieler-Ausfälle & Taktik-Rankings..."):
            st.session_state["kicktipp_results"] = run_kicktipp_live_pipeline()

    kt_data = st.session_state["kicktipp_results"]

    if kt_data:
        md_label = kt_data.get("matchday_label", "Aktueller Spieltag")
        matches_analyzed = kt_data.get("matches_analyzed", 0)
        inj_scraped = kt_data.get("total_injuries_scraped", 0)
        key_missing_cnt = kt_data.get("total_key_players_missing", 0)

        k_col1, k_col2, k_col3, k_col4 = st.columns(4)
        with k_col1:
            st.metric("📅 Spieltag", md_label)
        with k_col2:
            st.metric("⚽ Analysierte Saisonspiele", f"{matches_analyzed} Spiele")
        with k_col3:
            st.metric("🏥 Aktuelle Ausfälle (LigaInsider)", f"{inj_scraped} Spieler")
        with k_col4:
            st.metric("🚨 Fehlende Schlüsselspieler", f"{key_missing_cnt} Leistungsträger")

        st.markdown(f"#### ⚽ Berechnete Spieltagsergebnisse ({md_label})")
        for match in kt_data["predictions"]:
            det = match.get("details", {})
            h_rank = det.get("home_rank", "-")
            a_rank = det.get("away_rank", "-")
            h_pts = det.get("home_points", 0)
            a_pts = det.get("away_points", 0)
            h_goals = det.get("home_goals", "0:0")
            a_goals = det.get("away_goals", "0:0")
            probs = det.get("probabilities", {"home": 34, "draw": 33, "away": 33})
            lam_h = det.get("lambda_home", 1.3)
            lam_a = det.get("lambda_away", 1.1)
            insights = det.get("insights", [])

            with st.container():
                c1, c2, c3 = st.columns([3.2, 1.1, 4.7])
                with c1:
                    st.markdown(
                        f"**{match['home']}** <span style='color:#666;font-size:0.85rem;'>(#{h_rank}, {h_pts} Pkt, {h_goals} Tore)</span><br>"
                        f"<span style='color:#888;font-size:0.8rem;'>vs.</span><br>"
                        f"**{match['away']}** <span style='color:#666;font-size:0.85rem;'>(#{a_rank}, {a_pts} Pkt, {a_goals} Tore)</span>",
                        unsafe_allow_html=True,
                    )
                with c2:
                    st.markdown(
                        f"<div style='text-align:center;margin-top:0.35rem;'>"
                        f"<span style='background:#1b5e20;color:white;padding:5px 14px;border-radius:8px;font-weight:bold;font-size:1.15rem;'>{match['tip']}</span>"
                        f"<div style='font-size:0.75rem;color:#555;margin-top:4px;'>xG: {lam_h:.2f} : {lam_a:.2f}</div>"
                        f"<div style='font-size:0.72rem;color:#777;'>1X2: {probs['home']}% / {probs['draw']}% / {probs['away']}%</div>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                with c3:
                    if insights:
                        for ins in insights:
                            st.markdown(f"<div style='font-size:0.84rem;margin-bottom:2px;'>{ins}</div>", unsafe_allow_html=True)
                    else:
                        st.caption(match["analysis"])

                # Optionale Detail-Ansicht für letzte Ergebnisse & Taktik-Vergleich
                h_rec = det.get("home_recent_results", [])
                a_rec = det.get("away_recent_results", [])
                h_tac = det.get("home_tactical", {})
                a_tac = det.get("away_tactical", {})
                if h_rec or a_rec or h_tac:
                    with st.expander(f"🔎 Detail-Check: {match['home']} vs. {match['away']} (Saisonverlauf, Schlüsselspieler & Standards)"):
                        d_col1, d_col2 = st.columns(2)
                        with d_col1:
                            st.markdown(f"**🏠 {match['home']} (Platz #{h_rank})**")
                            st.caption(f"Form (L5): `{det.get('home_form_str', '—')}` | Letzte Spiele: {', '.join(h_rec) if h_rec else '—'}")
                            st.caption(
                                f"Standards-Offensivwert: `{h_tac.get('standard_attack_score', 78)}` | "
                                f"Standards-Anfälligkeit: `{h_tac.get('standard_vulnerability_score', 25)}` | "
                                f"Luftkampf: `{h_tac.get('aerial_quote', 50.0)}%`"
                            )
                            h_km = det.get("home_key_missing", [])
                            if h_km:
                                st.warning(
                                    "Fehlende Schlüsselspieler:\n"
                                    + "\n".join(f"- **{kp['name']}** ({kp['key_description']}, {kp.get('reason', 'Ausfall')})" for kp in h_km)
                                )
                            else:
                                st.success(f"Keine akuten Schlüsselspieler-Ausfälle ({det.get('home_total_injuries', 0)} Kader-Ausfälle gesamt)")
                        with d_col2:
                            st.markdown(f"**🚌 {match['away']} (Platz #{a_rank})**")
                            st.caption(f"Form (L5): `{det.get('away_form_str', '—')}` | Letzte Spiele: {', '.join(a_rec) if a_rec else '—'}")
                            st.caption(
                                f"Standards-Offensivwert: `{a_tac.get('standard_attack_score', 78)}` | "
                                f"Standards-Anfälligkeit: `{a_tac.get('standard_vulnerability_score', 25)}` | "
                                f"Luftkampf: `{a_tac.get('aerial_quote', 50.0)}%`"
                            )
                            a_km = det.get("away_key_missing", [])
                            if a_km:
                                st.warning(
                                    "Fehlende Schlüsselspieler:\n"
                                    + "\n".join(f"- **{kp['name']}** ({kp['key_description']}, {kp.get('reason', 'Ausfall')})" for kp in a_km)
                                )
                            else:
                                st.success(f"Keine akuten Schlüsselspieler-Ausfälle ({det.get('away_total_injuries', 0)} Kader-Ausfälle gesamt)")

                st.markdown("<hr style='margin: 0.35rem 0; border: none; border-top: 1px solid #eee;'>", unsafe_allow_html=True)

        # Übersichtstabelle mit aktuellen Tabellendaten + LigaInsider-Taktikwerten
        if kt_data.get("standings_table"):
            with st.expander("📊 Aktuelle Bundesliga-Tabelle & Taktische LigaInsider-Kennzahlen (Standards, Luftkampf, Großchancen, Schlüsselspieler)", expanded=False):
                st.dataframe(pd.DataFrame(kt_data["standings_table"]), use_container_width=True, hide_index=True)

        st.markdown("")
        st.markdown("#### 🏅 Saison-Bonusfragen (Mathematisch ermittelt)")
        b_col1, b_col2 = st.columns(2)
        for idx, bq in enumerate(kt_data["bonus_questions"]):
            target_col = b_col1 if idx % 2 == 0 else b_col2
            with target_col:
                with st.expander(f"{bq['question']}", expanded=True):
                    st.markdown(f"**Tipp:** `{bq['answer']}`")
                    st.markdown(f"**Wahrscheinlichkeit:** {bq['confidence']}")
                    st.caption(f"💡 *Begründung:* {bq['reasoning']}")
