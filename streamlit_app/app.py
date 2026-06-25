from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import streamlit as st
from supabase import create_client

DEFAULT_PAGE_SIZE = 1000
PREFERRED_COLS = [
    "Sheet",
    "Market",
    "Selection",
    "Name / Side",
    "Name",
    "Team",
    "Opponent",
    "Game",
    "GamePk",
    "Raw Projection Rank",
    "Raw HR Rank",
    "Market Projection %",
    "Projection %",
    "Market Model Score",
    "Model Score",
    "Market Confidence Tier",
    "Confidence Tier",
    "HR Coverage Lean?",
    "Final Bet Card Decision",
    "Should Bet?",
    "Recommended Usage",
    "Caution Flag",
    "Caution Reason",
    "Inclusion Reason",
    "Exclusion Reason",
    "Validation Status",
]
AUDIT_SHEET_KEYWORDS = ["Audit", "Notes", "Preflight", "Usage", "Lock"]


def get_secret(name: str, default: str | None = None) -> str | None:
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)


@st.cache_resource(show_spinner=False)
def get_supabase_client():
    url = get_secret("SUPABASE_URL")
    key = get_secret("SUPABASE_ANON_KEY") or get_secret("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        st.error("Missing Supabase credentials. Add SUPABASE_URL and SUPABASE_ANON_KEY to .streamlit/secrets.toml or Streamlit Cloud secrets.")
        st.stop()
    return create_client(url, key)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_runs(limit: int = 25) -> pd.DataFrame:
    supabase = get_supabase_client()
    result = (
        supabase.table("prediction_runs")
        .select("*")
        .order("published_at", desc=True)
        .limit(limit)
        .execute()
    )
    return pd.DataFrame(result.data or [])


@st.cache_data(ttl=60, show_spinner=False)
def fetch_rows(run_id: str, sheet_name: str | None = None, limit: int = DEFAULT_PAGE_SIZE) -> pd.DataFrame:
    supabase = get_supabase_client()
    query = (
        supabase.table("prediction_rows")
        .select("row_id, run_id, sheet_name, market, row_number, name, team, opponent, game, game_pk, raw_projection_rank, projection_percent, model_score, confidence_tier, recommended_usage, final_bet_card_decision, should_bet, caution_flag, validation_status, row_data")
        .eq("run_id", run_id)
        .order("sheet_name")
        .order("row_number")
        .limit(limit)
    )
    if sheet_name and sheet_name != "All sheets":
        query = query.eq("sheet_name", sheet_name)
    result = query.execute()
    return rows_to_dataframe(result.data or [])


def rows_to_dataframe(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    flat_rows = []
    for row in rows:
        base = {k: v for k, v in row.items() if k != "row_data"}
        row_data = row.get("row_data") or {}
        if isinstance(row_data, dict):
            merged = {**row_data, **base}
        else:
            merged = base
        flat_rows.append(merged)
    df = pd.DataFrame(flat_rows)
    # User-friendly fallback column aliases when JSON keys differ.
    rename_map = {
        "sheet_name": "Sheet",
        "market": "Market",
        "name": "Name",
        "team": "Team",
        "opponent": "Opponent",
        "game": "Game",
        "game_pk": "GamePk",
        "raw_projection_rank": "Raw Projection Rank (extracted)",
        "projection_percent": "Projection % (extracted)",
        "model_score": "Model Score (extracted)",
        "confidence_tier": "Confidence Tier (extracted)",
        "recommended_usage": "Recommended Usage (extracted)",
        "final_bet_card_decision": "Final Bet Card Decision (extracted)",
        "should_bet": "Should Bet? (extracted)",
        "caution_flag": "Caution Flag (extracted)",
        "validation_status": "Validation Status (extracted)",
        "row_number": "Workbook Row #",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    df = coalesce_duplicate_columns(df)
    df = fill_identity_fallbacks(df)
    df = add_selection_column(df)
    return reorder_columns(df)


def _is_blank_value(value: Any) -> bool:
    """Treat None/NaN/empty strings as blank for display fallback logic."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except Exception:
        pass
    return isinstance(value, str) and value.strip() == ""


def _first_nonblank(values: list[Any]) -> Any:
    for value in values:
        if not _is_blank_value(value):
            return value
    return None


def _display_safe(value: Any) -> Any:
    """Return a display-safe string value for Name/Team/Game fallback columns."""
    if _is_blank_value(value):
        return pd.NA
    return str(value)


def _looks_numeric_only(value: Any) -> bool:
    """True for values like 15.5 that should not be used as player/team/game names."""
    if _is_blank_value(value):
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip()
    if not text:
        return False
    try:
        float(text.replace("%", ""))
        return True
    except Exception:
        return False


def _first_nonblank_text(values: list[Any], reject_numeric: bool = False) -> Any:
    for value in values:
        if _is_blank_value(value):
            continue
        if reject_numeric and _looks_numeric_only(value):
            continue
        return str(value)
    return pd.NA


def _split_identity_value(value: Any) -> tuple[Any, Any, Any]:
    """Parse values like 'Player Name / TB / KC @ TB' or 'Player Name / TB'."""
    if _is_blank_value(value) or _looks_numeric_only(value):
        return (pd.NA, pd.NA, pd.NA)

    parts = [p.strip() for p in str(value).split("/") if p.strip()]
    name = parts[0] if len(parts) >= 1 and not _looks_numeric_only(parts[0]) else pd.NA
    team = parts[1] if len(parts) >= 2 and not _looks_numeric_only(parts[1]) else pd.NA
    game = parts[2] if len(parts) >= 3 else pd.NA
    return (name, team, game)


def add_selection_column(df: pd.DataFrame) -> pd.DataFrame:
    """Create a stable display column from whatever identity field the workbook uses."""
    if df.empty:
        return df

    df = df.copy()
    selection_candidates = [
        "Name",
        "Name / Side",
        "Name/Side",
        "Name / Team / Game",
        "Name/Team/Game",
        "Player / Team / Game",
        "Player/Team/Game",
        "Player Name",
        "Player",
        "Batter Name",
        "Batter",
        "Hitter Name",
        "Hitter",
        "Pitcher Name",
        "Recommended Side",
        "Predicted Winner",
        "Name / Team / Matchup",
        "Name/Team/Matchup",
    ]

    if "Selection" not in df.columns:
        df["Selection"] = pd.NA

    df["Selection"] = df["Selection"].astype("object")

    for idx in df.index:
        if _is_blank_value(df.at[idx, "Selection"]) or _looks_numeric_only(df.at[idx, "Selection"]):
            df.at[idx, "Selection"] = _first_nonblank_text(
                [df.at[idx, c] for c in selection_candidates if c in df.columns],
                reject_numeric=True,
            )

    return df


def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse duplicate column names using the first non-empty value across duplicates."""
    if df.empty:
        return df

    # Convert blank strings to NA before coalescing so empty extracted fields do not hide workbook values.
    df = df.replace(r"^\s*$", pd.NA, regex=True)

    if not df.columns.duplicated().any():
        return df

    cleaned = pd.DataFrame(index=df.index)

    for col in pd.unique(df.columns):
        matches = df.loc[:, df.columns == col]
        if isinstance(matches, pd.DataFrame) and matches.shape[1] > 1:
            cleaned[col] = matches.bfill(axis=1).iloc[:, 0]
        elif isinstance(matches, pd.DataFrame):
            cleaned[col] = matches.iloc[:, 0]
        else:
            cleaned[col] = matches

    return cleaned


def fill_identity_fallbacks(df: pd.DataFrame) -> pd.DataFrame:
    """Populate display identity columns from combined workbook fields when normalized fields are blank."""
    if df.empty:
        return df

    df = df.copy()

    # Fix: pandas/pyarrow may infer Name/Team/Game as strict string dtype.
    # Workbook fallback values can be numeric/mixed, so use object dtype before assignment.
    for display_col in ["Name", "Team", "Game"]:
        if display_col in df.columns:
            df[display_col] = df[display_col].astype("object")

    combined_candidates = [
        "Name / Team / Game",
        "Name/Team/Game",
        "Player / Team / Game",
        "Player/Team/Game",
        "Name / Side",
        "Name/Side",
        "Player / Side",
        "Player/Side",
        "Name / Team / Matchup",
        "Name/Team/Matchup",
    ]
    combined_col = next((c for c in combined_candidates if c in df.columns), None)

    # Make sure these display columns exist so we can fill them.
    for col in ["Name", "Team", "Game"]:
        if col not in df.columns:
            df[col] = pd.NA

    # Parse values like "Willson Contreras / BOS / BOS @ COL" or "Willson Contreras / TB".
    if combined_col:
        for idx in df.index:
            parsed_name, parsed_team, parsed_game = _split_identity_value(df.at[idx, combined_col])
            if _is_blank_value(df.at[idx, "Name"]):
                df.at[idx, "Name"] = parsed_name
            if _is_blank_value(df.at[idx, "Team"]):
                df.at[idx, "Team"] = parsed_team
            if _is_blank_value(df.at[idx, "Game"]):
                df.at[idx, "Game"] = parsed_game

    # Only use true identity fields as fallbacks.
    # Do NOT use numeric summary fields like "Top Projection" as a player name.
    name_fallback_cols = [
        "Source Name",
        "Parsed Name",
        "Player Name",
        "Player",
        "Batter Name",
        "Batter",
        "Hitter Name",
        "Hitter",
        "Pitcher Name",
        "Name / Side",
        "Name/Side",
        "Player / Side",
        "Player/Side",
        "Selection",
        "Name / Team / Game",
        "Name/Team/Game",
        "Player / Team / Game",
        "Player/Team/Game",
    ]
    team_fallback_cols = [
        "Player Team",
        "Batter Team",
        "Hitter Team",
        "Pitcher Team",
        "Team Abbrev",
        "TeamAbbrev",
        "Side",
        "Recommended Side",
        "Predicted Winner",
        "Away Team",
        "Home Team",
    ]
    game_fallback_cols = [
        "Matchup",
        "Game Label",
        "Game",
        "Doubleheader Safety Key",
    ]

    for idx in df.index:
        if _is_blank_value(df.at[idx, "Name"]):
            df.at[idx, "Name"] = _first_nonblank_text([df.at[idx, c] for c in name_fallback_cols if c in df.columns], reject_numeric=True)
        if _is_blank_value(df.at[idx, "Team"]):
            df.at[idx, "Team"] = _first_nonblank_text([df.at[idx, c] for c in team_fallback_cols if c in df.columns], reject_numeric=True)
        if _is_blank_value(df.at[idx, "Game"]):
            df.at[idx, "Game"] = _first_nonblank_text([df.at[idx, c] for c in game_fallback_cols if c in df.columns], reject_numeric=False)

    # Clean placeholder strings that came from astype(str) on missing combined values.
    for col in ["Name", "Team", "Game"]:
        df[col] = df[col].replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA, "NaT": pd.NA})

    return df


def _dedupe_preserve_order(columns: list[str]) -> list[str]:
    seen = set()
    out = []
    for col in columns:
        if col not in seen:
            out.append(col)
            seen.add(col)
    return out


def ensure_unique_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure pyarrow/Streamlit can render the dataframe by removing duplicate labels."""
    if df.empty:
        return df
    if not df.columns.duplicated().any():
        return df

    cleaned = pd.DataFrame(index=df.index)
    for col in pd.unique(df.columns):
        matches = df.loc[:, df.columns == col]
        if isinstance(matches, pd.DataFrame) and matches.shape[1] > 1:
            cleaned[col] = matches.bfill(axis=1).iloc[:, 0]
        elif isinstance(matches, pd.DataFrame):
            cleaned[col] = matches.iloc[:, 0]
        else:
            cleaned[col] = matches
    return cleaned


def to_streamlit_safe_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert a dataframe to a Streamlit/Arrow-safe display frame.

    New model workbooks can place strings like Neutral / Manual Review / Pass
    in columns that pandas initially inferred as numeric. Streamlit renders
    dataframes through pyarrow, and pyarrow can fail when a single column
    mixes numeric values with text. This function is display-only: filtering
    and sorting should happen before this conversion.
    """
    if df is None or df.empty:
        return df

    safe = ensure_unique_columns(df.copy())
    safe.columns = [str(c) for c in safe.columns]

    def _safe_cell(value: Any) -> str:
        if value is None:
            return ""
        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass
        return str(value)

    for col in safe.columns:
        safe[col] = safe[col].map(_safe_cell)

    return safe


def reorder_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = ensure_unique_columns(df)

    preferred_existing = _dedupe_preserve_order([c for c in PREFERRED_COLS if c in df.columns])
    extracted_existing = _dedupe_preserve_order([
        c for c in df.columns
        if (c.endswith("(extracted)") or c == "Workbook Row #") and c not in preferred_existing
    ])
    remaining = _dedupe_preserve_order([
        c for c in df.columns
        if c not in preferred_existing and c not in extracted_existing and c not in {"row_id", "run_id"}
    ])

    ordered_cols = _dedupe_preserve_order(preferred_existing + extracted_existing + remaining)
    return ensure_unique_columns(df[ordered_cols])


def format_run_label(row: pd.Series) -> str:
    slate = row.get("slate_date") or "No slate date"
    published = row.get("published_at") or "No publish time"
    latest = "LATEST — " if row.get("is_latest") else ""
    output = row.get("output_workbook_name") or "Output workbook"
    return f"{latest}{slate} | {output} | {published}"


def render_metric_cards(run: pd.Series):
    cols = st.columns(4)
    cols[0].metric("Lineup hitters", value=run.get("eligible_hitter_count") or "—")
    cols[1].metric("Eligible games", value=run.get("eligible_game_count") or "—")
    cols[2].metric("Strikeouts run", value="Yes" if run.get("strikeouts_run") else "No")
    cols[3].metric("Validation", value=run.get("validation_status") or "—")

    pick_cols = st.columns(4)
    pick_cols[0].write(f"**Top HR:** {run.get('top_hr_pick') or '—'}")
    pick_cols[1].write(f"**Top Hit:** {run.get('top_hit_pick') or '—'}")
    pick_cols[2].write(f"**Top RBI:** {run.get('top_rbi_pick') or '—'}")
    pick_cols[3].write(f"**Top ML/K:** {run.get('top_ml_pick') or run.get('top_k_prop') or '—'}")

    st.info(f"Best HR environment: {run.get('best_hr_environment') or '—'}")


def apply_filters(df: pd.DataFrame, key_prefix: str = "main") -> pd.DataFrame:
    if df.empty:
        return df
    filtered = df.copy()
    with st.sidebar:
        st.header("Filters")
        if "Market" in filtered.columns:
            markets = sorted([str(x) for x in filtered["Market"].dropna().unique()])
            selected = st.multiselect("Market", markets, default=[], key=f"{key_prefix}_market")
            if selected:
                filtered = filtered[filtered["Market"].astype(str).isin(selected)]

        for label, col in [("Team", "Team"), ("Game", "Game"), ("Recommended Usage", "Recommended Usage"), ("Final Decision", "Final Bet Card Decision")]:
            if col in filtered.columns:
                values = sorted([str(x) for x in filtered[col].dropna().unique()])
                selected = st.multiselect(label, values, default=[], key=f"{key_prefix}_{col}")
                if selected:
                    filtered = filtered[filtered[col].astype(str).isin(selected)]

        search = st.text_input("Search name/team/game", key=f"{key_prefix}_search")
        if search:
            search_l = search.lower().strip()
            text_cols = [c for c in ["Name", "Team", "Opponent", "Game", "Market"] if c in filtered.columns]
            if text_cols:
                mask = filtered[text_cols].astype(str).apply(lambda s: s.str.lower().str.contains(search_l, na=False)).any(axis=1)
                filtered = filtered[mask]

        sort_candidates = [c for c in filtered.columns if c in [
            "Raw Projection Rank", "Raw HR Rank", "Market Projection %", "Projection %", "Market Model Score", "Model Score", "Workbook Row #",
            "Raw Projection Rank (extracted)", "Projection % (extracted)", "Model Score (extracted)"
        ]]
        if sort_candidates:
            sort_col = st.selectbox("Sort by", ["No sort"] + sort_candidates, key=f"{key_prefix}_sort")
            if sort_col != "No sort":
                ascending = "Rank" in sort_col or sort_col == "Workbook Row #"
                filtered[sort_col] = pd.to_numeric(filtered[sort_col], errors="ignore")
                filtered = filtered.sort_values(sort_col, ascending=ascending)
    return filtered


def render_sheet_table(run_id: str, sheet_name: str | None = None, title: str = "Rows", key_prefix: str | None = None):
    with st.spinner("Loading prediction rows..."):
        df = fetch_rows(run_id, sheet_name=sheet_name, limit=DEFAULT_PAGE_SIZE)
    if df.empty:
        st.warning("No rows found for this selection.")
        return
    safe_key = key_prefix or str(sheet_name or title or "rows").replace(" ", "_").replace("/", "_")
    filtered = apply_filters(df, key_prefix=safe_key)
    st.caption(f"Showing {len(filtered):,} of {len(df):,} loaded rows. Increase DEFAULT_PAGE_SIZE in app.py if needed.")
    st.dataframe(to_streamlit_safe_df(filtered), use_container_width=True, hide_index=True, height=650)


def available_sheet_names(run_id: str) -> list[str]:
    """Return available workbook sheet names for the selected run."""
    df = fetch_rows(run_id, sheet_name=None, limit=DEFAULT_PAGE_SIZE)
    if df.empty or "Sheet" not in df.columns:
        return []
    return sorted([str(x) for x in df["Sheet"].dropna().unique()])


def find_best_sheet(available_sheets: list[str], candidates: list[str]) -> str | None:
    """Find exact or loose sheet-name match from workbook sheet names."""
    if not available_sheets:
        return None

    available_lower = {s.lower().strip(): s for s in available_sheets}

    # Exact normalized match first.
    for candidate in candidates:
        key = candidate.lower().strip()
        if key in available_lower:
            return available_lower[key]

    # Loose contains match second.
    for candidate in candidates:
        c = candidate.lower().strip()
        for sheet in available_sheets:
            s = sheet.lower().strip()
            if c in s or s in c:
                return sheet

    # Token match last.
    for candidate in candidates:
        tokens = [t for t in re.split(r"[^a-z0-9]+", candidate.lower()) if t]
        if not tokens:
            continue
        for sheet in available_sheets:
            s = sheet.lower()
            if all(token in s for token in tokens):
                return sheet

    return None


def render_prediction_section(
    run_id: str,
    available_sheets: list[str],
    title: str,
    sheet_candidates: list[str],
    key_prefix: str,
    description: str | None = None,
):
    """Render a dashboard section from the first matching workbook sheet."""
    matched_sheet = find_best_sheet(available_sheets, sheet_candidates)

    st.subheader(title)
    if description:
        st.caption(description)

    if matched_sheet is None:
        st.warning(f"No matching sheet found for: {title}")
        with st.expander("Sheet names checked"):
            st.write(sheet_candidates)
        with st.expander("Available sheets seen for this run"):
            st.write(available_sheets or ["No sheet rows found for this run"])
        return

    st.caption(f"Source sheet: {matched_sheet}")
    render_sheet_table(run_id, sheet_name=matched_sheet, title=title, key_prefix=key_prefix)





def main():
    st.set_page_config(page_title="HR Projections 26", layout="wide")
    st.title("HR Projections 26 Portal")
    st.caption("App version: visible prediction tabs v15 — locked 10 display tabs with workbook source fallback")
    st.caption("Dashboard displays only the 10 requested tabs. If an exact workbook sheet is missing, the app pulls from the closest available imported workbook sheet.")

    runs = fetch_runs()
    if runs.empty:
        st.warning("No prediction runs have been published yet. Run scripts/import_output_workbook.py after creating a model output workbook.")
        return

    latest_idx = 0
    if "is_latest" in runs.columns and runs["is_latest"].fillna(False).any():
        latest_idx = int(runs[runs["is_latest"].fillna(False)].index[0])

    run_labels = [format_run_label(row) for _, row in runs.iterrows()]
    selected_label = st.selectbox("Published run", run_labels, index=latest_idx)
    selected_row = runs.iloc[run_labels.index(selected_label)]
    run_id = selected_row["run_id"]

    render_metric_cards(selected_row)

    st.divider()

    locked_tabs = [
        "Strong Market Signal Board",
        "Final Bet Card",
        "Top 3 HR by Game",
        "Core HR Top 30",
        "Longshots HR",
        "Best Game HR Coverage",
        "Top 2 Confidence Candidates",
        "Risk-Adjusted Parlays",
        "Moneyline Predictions",
        "Strikeout Props",
    ]

    # Visible tab names stay locked exactly as requested.
    # Source sheets use exact names first, then fallback to the current imported workbook sheet names.
    source_candidates = {
        "Strong Market Signal Board": [
            "Strong Market Signal Board",
            "Combined Market Rankings",
        ],
        "Final Bet Card": [
            "Final Bet Card",
            "Active Pregame Recs",
        ],
        "Top 3 HR by Game": [
            "Top 3 HR by Game",
            "All HR Rankings",
        ],
        "Core HR Top 30": [
            "Core HR Top 30",
            "All HR Rankings",
        ],
        "Longshots HR": [
            "Longshots HR",
            "All HR Rankings",
        ],
        "Best Game HR Coverage": [
            "Best Game HR Coverage",
        ],
        "Top 2 Confidence Candidates": [
            "Top 2 Confidence Candidates",
            "Combined Market Rankings",
            "Active Pregame Recs",
        ],
        "Risk-Adjusted Parlays": [
            "Risk-Adjusted Parlays",
            "Active Pregame Recs",
            "Combined Market Rankings",
        ],
        "Moneyline Predictions": [
            "Moneyline Predictions",
            "Combined Market Rankings",
        ],
        "Strikeout Props": [
            "Strikeout Props",
            "Combined Market Rankings",
            "Active Pregame Recs",
        ],
    }

    available_sheets = available_sheet_names(run_id)

    with st.expander("Available workbook sheets", expanded=False):
        if available_sheets:
            st.write(available_sheets)
        else:
            st.warning("No sheet names found for this run.")

    st.subheader("Dashboard Tabs")
    st.caption("Only the 10 locked dashboard tabs are shown. Each tab displays its exact sheet if present, otherwise a source fallback sheet is used.")

    dashboard_tabs = st.tabs(locked_tabs)

    for tab_name, tab in zip(locked_tabs, dashboard_tabs):
        with tab:
            st.subheader(tab_name)

            candidates = source_candidates.get(tab_name, [tab_name])
            matched_sheet = None

            # Prefer exact sheet name first.
            if tab_name in available_sheets:
                matched_sheet = tab_name
            else:
                matched_sheet = find_best_sheet(available_sheets, candidates)

            if matched_sheet is None:
                st.warning(f"No source workbook sheet found for: {tab_name}")
                with st.expander("Sheet names checked", expanded=False):
                    st.write(candidates)
                with st.expander("Available sheets seen for this run", expanded=True):
                    st.write(available_sheets or ["No sheet rows found for this run"])
                continue

            if matched_sheet != tab_name:
                st.info(f"Visible tab: {tab_name} | Source workbook sheet used: {matched_sheet}")
            else:
                st.caption(f"Source sheet: {matched_sheet}")

            render_sheet_table(
                run_id,
                sheet_name=matched_sheet,
                title=tab_name,
                key_prefix=f"locked_display_{re.sub(r'[^A-Za-z0-9_]+', '_', tab_name)}",
            )


if __name__ == "__main__":
    main()
