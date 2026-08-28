"""
app.py – Streamlit Dashboard für den Kickbase Arena Optimizer & KickTipp
Features:
- Kickbase Lineup Optimization mit ILP-Solver
- Spieler-Sperrliste (Sperren & Re-Optimize)
- Formations-Vergleich
- Permanente manuelle Marktwert-Korrekturen (gespeichert in custom_market_values.json)
- Live LigaInsider Scraping & KickTipp-Spieltagsprognosen
"""

import json
import os
import streamlit as st
import pandas as pd
from data_collector import run_pipeline, KickbaseAPIError, LigaInsiderError
from optimizer import optimize_lineup, get_valid_formations, OptimizationError
from kicktipp_engine import run_kicktipp_live_pipeline

# ---------------------------------------------------------------------------
# Page Config & Persistence Helpers
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="⚽ Kickbase & KickTipp Optimizer",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_MV_FILE = "custom_market_values.json"


def load_custom_market_values() -> dict:
    """Lädt gespeicherte Marktwerte aus der lokalen JSON-Datei."""
    if os.path.exists(CUSTOM_MV_FILE):
        try:
            with open(CUSTOM_MV_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_custom_market_values(data: dict):
    """Speichert manuelle Marktwerte dauerhaft auf die Festplatte."""
    with open(CUSTOM_MV_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def apply_custom_market_values(df: pd.DataFrame, custom_mvs: dict) -> pd.DataFrame:
    """Wendet die manuell gespeicherten Marktwerte auf das DataFrame an."""
    if df is None or df.empty or not custom_mvs:
        return df

    df = df.copy()
    for pid_or_name, val in custom_mvs.items():
        # Zuerst nach ID suchen
        mask_id = df["id"].astype(str) == str(pid_or_name)
        if mask_id.any():
            df.loc[mask_id, "market_value"] = float(val)
        else:
            # Fallback: Nach Spielernamen matchen
            mask_name = df["name"].astype(str).str.strip().str.lower() == str(pid_or_name).strip().lower()
            if mask_name.any():
                df.loc[mask_name, "market_value"] = float(val)
    return df


# ---------------------------------------------------------------------------
# State Initialization
# ---------------------------------------------------------------------------
if "result" not in st.session_state:
    st.session_state["result"] = None
if "all_players" not in st.session_state:
    st.session_state["all_players"] = None
if "optimizable" not in st.session_state:
    st.session_state["optimizable"] = None
if "locked_out_players" not in st.session_state:
    st.session_state["locked_out_players"] = {}
if "custom_mvs" not in st.session_state:
    st.session_state["custom_mvs"] = load_custom_market_values()
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


def render_player_card(player: pd.Series, is_captain: bool = False) -> str:
    captain_html = '<span class="captain-badge">C</span>' if is_captain else ""
    return f"""
    <div class="player-card">
        <div class="name">{player['name']}{captain_html}</div>
        <div class="team">{player.get('team', '')}</div>
        <div class="xp">⚡ {player['xp']:.1f} xP</div>
        <div class="mv">{format_market_value(player['market_value'])}</div>
    </div>
    """


def render_pitch(lineup: pd.DataFrame, captain_id: str):
    st.markdown('<div class="section-header">⚽ Optimale Aufstellung</div>', unsafe_allow_html=True)
    positions = {
        1: lineup[lineup["position"] == 1],
        2: lineup[lineup["position"] == 2],
        3: lineup[lineup["position"] == 3],
        4: lineup[lineup["position"] == 4],
    }
    position_labels = {4: "🔴 STURM", 3: "🟡 MITTELFELD", 2: "🔵 ABWEHR", 1: "🟢 TORWART"}

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
            is_cap = str(player.get("id", "")) == str(captain_id) or player.get("is_captain", False)
            with cols[idx]:
                st.markdown(render_player_card(player, is_cap), unsafe_allow_html=True)


def render_kpis(result: dict):
    st.markdown('<div class="section-header">📊 KPI-Übersicht</div>', unsafe_allow_html=True)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(f'<div class="kpi-card"><div class="value">⚡ {result["total_xp"]:.1f}</div><div class="label">Erwartete Punkte (inkl. C)</div></div>', unsafe_allow_html=True)
    with col2:
        st.markdown(f'<div class="kpi-card"><div class="value">💰 {format_market_value(result["total_market_value"])}</div><div class="label">Genutztes Budget</div></div>', unsafe_allow_html=True)
    with col3:
        st.markdown(f'<div class="kpi-card"><div class="value">🏦 {format_market_value(result["budget_remaining"])}</div><div class="label">Rest-Budget</div></div>', unsafe_allow_html=True)
    with col4:
        st.markdown(f'<div class="kpi-card"><div class="value">📐 {result["formation"]}</div><div class="label">Formation</div></div>', unsafe_allow_html=True)


def render_detail_table(lineup: pd.DataFrame, captain_id: str):
    st.markdown('<div class="section-header">📋 Detaillierte Aufstellung</div>', unsafe_allow_html=True)
    display_df = lineup.copy()
    display_df["Kapitän"] = display_df.apply(
        lambda r: "👑 C" if (str(r.get("id", "")) == str(captain_id) or r.get("is_captain", False)) else "", axis=1
    )
    display_df["Marktwert"] = display_df["market_value"].apply(format_market_value)
    display_df["Position"] = display_df["position_label"] if "position_label" in display_df.columns else display_df["position"]
    display_df["xP"] = display_df["xp"].round(1)
    display_df["Ø Punkte"] = display_df["average_points"].round(1)
    display_df["P(Start)"] = display_df["p_start"].apply(lambda p: f"{p:.0%}")
    display_df["LigaInsider"] = display_df.get("li_status", "—")

    columns_to_show = ["Kapitän", "Name", "Verein", "Position", "Marktwert", "Ø Punkte", "P(Start)", "xP", "LigaInsider"]
    display_df = display_df.rename(columns={"name": "Name", "team": "Verein"})
    existing_cols = [c for c in columns_to_show if c in display_df.columns]
    st.dataframe(display_df[existing_cols], use_container_width=True, hide_index=True, height=430)


# ---------------------------------------------------------------------------
# Sidebar (Lineup & Persistenz)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("# ⚽ Kickbase & KickTipp")
    st.markdown("### Manager Dashboard")
    st.markdown("---")

    st.markdown("#### 🔑 Kickbase Zugangsdaten")
    email = st.text_input("Kickbase E-Mail", value=st.session_state.get("kb_email", ""), placeholder="deine@email.de")
    password = st.text_input("Kickbase Passwort", value=st.session_state.get("kb_password", ""), type="password", placeholder="Dein Passwort")

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

    # Gesperrte Spieler Übersicht
    if st.session_state["locked_out_players"]:
        st.markdown("---")
        st.markdown("#### 🚫 Gesperrte Spieler")
        for pid, pname in list(st.session_state["locked_out_players"].items()):
            c1, c2 = st.columns([3, 1])
            c1.caption(f"❌ {pname}")
            if c2.button("Entsperren", key=f"unlock_side_{pid}"):
                del st.session_state["locked_out_players"][pid]
                st.rerun()

        if st.button("🗑️ Alle Sperren löschen", use_container_width=True):
            st.session_state["locked_out_players"] = {}
            st.rerun()

    # Gespeicherte manuelle Marktwerte
    st.markdown("---")
    st.markdown("#### 💰 Gespeicherte Marktwerte")
    if st.session_state["custom_mvs"]:
        for p_key, p_val in list(st.session_state["custom_mvs"].items()):
            c1, c2 = st.columns([3, 1])
            c1.caption(f"{p_key}: **{format_market_value(p_val)}**")
            if c2.button("❌", key=f"del_mv_{p_key}", help="Korrektur löschen"):
                del st.session_state["custom_mvs"][p_key]
                save_custom_market_values(st.session_state["custom_mvs"])
                if st.session_state.get("optimizable") is not None:
                    all_p = st.session_state["all_players"].copy()
                    st.session_state["optimizable"] = apply_custom_market_values(all_p, st.session_state["custom_mvs"])
                st.rerun()

        if st.button("🗑️ Alle Korrekturen löschen", use_container_width=True):
            st.session_state["custom_mvs"] = {}
            save_custom_market_values({})
            st.rerun()
    else:
        st.caption("Keine manuellen Marktwerte hinterlegt.")


# ---------------------------------------------------------------------------
# Hauptbereich mit Tabs
# ---------------------------------------------------------------------------
st.markdown(
    "<h1 style='text-align:center;margin-bottom:0;'>⚽ Kickbase Arena & KickTipp</h1>"
    "<p style='text-align:center;color:#666;margin-top:0.2rem;'>"
    "Optimale Aufstellungen & datenbasierte Spieltagsprognosen</p>",
    unsafe_allow_html=True,
)

tab_optimizer, tab_kicktipp = st.tabs(["🧮 Lineup Optimizer", "🎯 KickTipp Prognosen"])


# ===========================================================================
# TAB 1: KICKBASE LINEUP OPTIMIZER
# ===========================================================================
with tab_optimizer:
    # 1. Pipeline ausführen wenn Login geklickt
    if sync_clicked:
        if not email or not password:
            st.error("⚠️ Bitte E-Mail und Passwort in der Sidebar eingeben.")
        else:
            st.session_state["kb_email"] = email
            st.session_state["kb_password"] = password

            with st.spinner("🔄 Kickbase-Login & Spieler-Abruf..."):
                try:
                    all_players, optimizable = run_pipeline(email, password)
                    # Manuell gespeicherte Marktwerte anwenden
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
                    result = optimize_lineup(
                        st.session_state["optimizable"],
                        budget=budget_value,
                        formation=formation_key,
                        excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                    )
                    st.session_state["result"] = result
                except OptimizationError as e:
                    st.error(f"❌ {e}")
                    st.stop()

    # 2. Wenn Daten geladen sind: Manuelle Marktwert-Änderung & Sperrliste
    if st.session_state.get("optimizable") is not None and st.session_state.get("result") is not None:
        current_lineup = st.session_state["result"]["lineup"]

        # --- BEREICH: MARKTWERT MANUELL ANPASSEN ---
        with st.expander("✏️ Marktwert eines Spielers manuell anpassen & dauerhaft speichern", expanded=False):
            opt_df = st.session_state["optimizable"]
            player_names = sorted(opt_df["name"].dropna().unique().tolist())
            
            edit_col1, edit_col2, edit_col3 = st.columns([2, 2, 1])
            with edit_col1:
                selected_player = st.selectbox("Spieler wählen:", options=player_names, index=0 if player_names else None)
            
            current_val = 0.0
            if selected_player:
                match_row = opt_df[opt_df["name"] == selected_player]
                if not match_row.empty:
                    current_val = float(match_row.iloc[0]["market_value"])

            with edit_col2:
                new_mv_input = st.number_input(
                    "Neuer Marktwert in € (z. B. 2800000):",
                    value=int(current_val),
                    step=100_000,
                    format="%d"
                )

            with edit_col3:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("💾 Speichern & Anwenden", type="primary", use_container_width=True):
                    if selected_player and new_mv_input > 0:
                        st.session_state["custom_mvs"][selected_player] = float(new_mv_input)
                        save_custom_market_values(st.session_state["custom_mvs"])
                        
                        st.session_state["optimizable"] = apply_custom_market_values(
                            st.session_state["optimizable"],
                            st.session_state["custom_mvs"]
                        )
                        
                        try:
                            new_result = optimize_lineup(
                                st.session_state["optimizable"],
                                budget=budget_value,
                                formation=formation_key,
                                excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                            )
                            st.session_state["result"] = new_result
                            st.success(f"Marktwert für {selected_player} auf {format_market_value(new_mv_input)} gesetzt!")
                            st.rerun()
                        except OptimizationError as e:
                            st.error(f"❌ {e}")

        # --- BEREICH: SPIELER SPERREN ---
        st.markdown("---")
        st.markdown("### 🚫 Spieler sperren & Neu berechnen")
        
        player_choices = {
            f"{row['name']} ({row.get('team', '')} - {row['position_label']})": str(row['id'])
            for _, row in current_lineup.iterrows()
        }

        selected_to_block = st.multiselect(
            "Wähle Spieler aus der aktuellen Elf aus, die du ausschließen willst:",
            options=list(player_choices.keys()),
            placeholder="Spieler zum Sperren auswählen...",
        )

        col_btn1, col_btn2 = st.columns([2, 1])
        with col_btn1:
            if st.button("🚫 Ausgewählte Spieler sperren & Neue Elf berechnen", type="primary", use_container_width=True):
                if selected_to_block:
                    for label in selected_to_block:
                        p_id = player_choices[label]
                        p_name = label.split(" (")[0]
                        st.session_state["locked_out_players"][p_id] = p_name

                    try:
                        new_result = optimize_lineup(
                            st.session_state["optimizable"],
                            budget=budget_value,
                            formation=formation_key,
                            excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                        )
                        st.session_state["result"] = new_result
                        st.rerun()
                    except OptimizationError as e:
                        st.error(f"❌ {e}")

        with col_btn2:
            if st.button("🔄 Ohne Änderungen neu optimieren", use_container_width=True):
                new_result = optimize_lineup(
                    st.session_state["optimizable"],
                    budget=budget_value,
                    formation=formation_key,
                    excluded_player_ids=list(st.session_state["locked_out_players"].keys()),
                )
                st.session_state["result"] = new_result
                st.rerun()

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
    st.caption("Scraped LigaInsider nach aktuellen Ausfällen, Verletzungen und berechnet Stärketrends.")

    btn_scrape_kt = st.button("🔄 LigaInsider jetzt live analysieren & Tipps berechnen", type="primary")

    if btn_scrape_kt or st.session_state["kicktipp_results"] is None:
        with st.spinner("🕵️ Scrape LigaInsider-Ausfälle & berechne Spieltagsprognosen..."):
            st.session_state["kicktipp_results"] = run_kicktipp_live_pipeline()

    kt_data = st.session_state["kicktipp_results"]

    if kt_data:
        st.success(f"✅ Analyse abgeschlossen: {kt_data['total_injuries_scraped']} Ausfälle/Statusmeldungen von LigaInsider berücksichtigt.")
        
        st.markdown("#### ⚽ Berechnete Spieltagsergebnisse")
        for match in kt_data["predictions"]:
            with st.container():
                c1, c2, c3 = st.columns([3, 1, 4])
                with c1:
                    st.markdown(f"**{match['home']}** – **{match['away']}**")
                with c2:
                    st.markdown(
                        f"<span style='background:#1b5e20;color:white;padding:3px 10px;border-radius:6px;font-weight:bold;font-size:1.05rem;'>{match['tip']}</span>",
                        unsafe_allow_html=True,
                    )
                with c3:
                    st.caption(match["analysis"])
                st.markdown("<hr style='margin: 0.2rem 0; border: none; border-top: 1px solid #eee;'>", unsafe_allow_html=True)

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