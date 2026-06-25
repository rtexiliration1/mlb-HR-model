from __future__ import annotations

import ast
import json
import os
from typing import Any

import pandas as pd
import streamlit as st
from supabase import create_client


LOCKED_TABS = [
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
    key = get_secret("SUPABASE_ANON_KEY")
    if not url or not key:
        st.error("Missing Streamlit secrets: SUPABASE_URL and SUPABASE_ANON_KEY.")
        st.stop()
    return create_client(url, key)


@st.cache_data(ttl=30, show_spinner=False)
def fetch_runs(limit: int = 25) -> list[dict[str, Any]]:
    supabase = get_supabase_client()
    result = (
        supabase.table("prediction_runs")
        .select("*")
        .order("published_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


@st.cache_data(ttl=30, show_spinner=False)
def fetch_prediction_rows(run_id: str, limit: int = 10000) -> list[dict[str, Any]]:
    supabase = get_supabase_client()
    result = (
        supabase.table("prediction_rows")
        .select("*")
        .eq("run_id", run_id)
        .order("sheet_name")
        .order("row_number")
        .limit(limit)
        .execute()
    )
    return result.data or []


def parse_row_data(payload: Any) -> dict[str, Any]:
    """Parse row_data from Supabase into a flat dict.

    Handles:
    - dict returned from jsonb
    - JSON string
    - double-encoded JSON string
    - Python-literal-like dict string
    - list containing a single dict
    """
    if payload is None:
        return {}

    value = payload

    for _ in range(4):
        if isinstance(value, dict):
            return value

        if isinstance(value, list):
            if len(value) == 1 and isinstance(value[0], dict):
                return value[0]
            return {"row_data_json": json.dumps(value, ensure_ascii=False)}

        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return {}

            try:
                value = json.loads(raw)
                continue
            except Exception:
                pass

            try:
                value = ast.literal_eval(raw)
                continue
            except Exception:
                return {"row_data_raw": raw}

        return {"row_data_raw": str(value)}

    if isinstance(value, dict):
        return value

    return {"row_data_raw": str(value)}


def flatten_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    flat = []
    for row in rows:
        base = {k: v for k, v in row.items() if k != "row_data"}
        parsed = parse_row_data(row.get("row_data"))

        # Normalize nested row_data only. Keep original workbook column names when possible.
        if parsed:
            parsed_df = pd.json_normalize(parsed, sep=".")
            parsed_flat = parsed_df.iloc[0].to_dict()
        else:
            parsed_flat = {}

        merged = {
            "Sheet": base.get("sheet_name"),
            "Workbook Row #": base.get("row_number"),
            **parsed_flat,
        }

        # Add extracted DB fields only if they contain data and are not already present.
        extracted_map = {
            "market": "Market",
            "name": "Name",
            "team": "Team",
            "opponent": "Opponent",
            "game": "Game",
            "game_pk": "GamePk",
            "raw_projection_rank": "Raw Projection Rank (DB)",
            "projection_percent": "Projection % (DB)",
            "model_score": "Model Score (DB)",
            "confidence_tier": "Confidence Tier (DB)",
            "recommended_usage": "Recommended Usage (DB)",
            "final_bet_card_decision": "Final Bet Card Decision (DB)",
            "should_bet": "Should Bet? (DB)",
            "caution_flag": "Caution Flag (DB)",
            "validation_status": "Validation Status (DB)",
        }

        for src, dst in extracted_map.items():
            val = base.get(src)
            if val is not None and str(val).strip() != "" and dst not in merged:
                merged[dst] = val

        flat.append(merged)

    if not flat:
        return pd.DataFrame()

    df = pd.DataFrame(flat)

    # Drop fully empty columns, but keep sheet/row markers.
    keep = []
    for col in df.columns:
        if col in {"Sheet", "Workbook Row #"}:
            keep.append(col)
            continue

        series = df[col].map(lambda v: "" if v is None or str(v) in {"nan", "None", "<NA>", "NaT"} else str(v).strip())
        if series.ne("").any():
            keep.append(col)

    df = df[keep]

    # Create a readable selection column if possible.
    candidate_cols = [
        "Selection",
        "Name",
        "Name / Side",
        "Name/Side",
        "Player",
        "Player Name",
        "Batter",
        "Batter Name",
        "Pitcher",
        "Pitcher Name",
        "Recommended Side",
        "Predicted Winner",
    ]
    if "Selection" not in df.columns:
        for c in candidate_cols:
            if c in df.columns:
                df.insert(2, "Selection", df[c])
                break

    preferred = [
        "Sheet",
        "Workbook Row #",
        "Selection",
        "Market",
        "Name / Side",
        "Name",
        "Team",
        "Opponent",
        "Game",
        "Raw Projection Rank",
        "Raw HR Rank",
        "Projection %",
        "Market Projection %",
        "Model Score",
        "Market Model Score",
        "Confidence Tier",
        "Market Confidence Tier",
        "Final Bet Card Decision",
        "Should Bet?",
        "Recommended Usage",
        "Caution Flag",
        "Caution Reason",
        "Inclusion Reason",
        "Exclusion Reason",
    ]
    ordered = [c for c in preferred if c in df.columns] + [c for c in df.columns if c not in preferred]
    df = df[ordered]

    return df


def safe_display_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out.columns = [str(c) for c in out.columns]

    # Ensure duplicate columns do not break Streamlit.
    seen = {}
    unique_cols = []
    for col in out.columns:
        count = seen.get(col, 0)
        if count == 0:
            unique_cols.append(col)
        else:
            unique_cols.append(f"{col}.{count}")
        seen[col] = count + 1
    out.columns = unique_cols

    for col in out.columns:
        out[col] = out[col].map(lambda v: "" if v is None or str(v) in {"nan", "None", "<NA>", "NaT"} else str(v))
    return out


def format_run_label(run: dict[str, Any]) -> str:
    latest = "LATEST — " if run.get("is_latest") else ""
    slate = run.get("slate_date") or "No slate date"
    output = run.get("output_workbook_name") or run.get("source_workbook_name") or "Workbook"
    published = run.get("published_at") or "No published_at"
    return f"{latest}{slate} | {output} | {published}"


def find_sheet(available: list[str], desired: str) -> str | None:
    desired_norm = desired.strip().casefold()
    for sheet in available:
        if str(sheet).strip().casefold() == desired_norm:
            return sheet
    return None


def render_metric_cards(run: dict[str, Any]):
    cols = st.columns(4)
    cols[0].metric("Lineup hitters", run.get("eligible_hitter_count") or "—")
    cols[1].metric("Eligible games", run.get("eligible_game_count") or "—")
    cols[2].metric("Strikeouts run", "Yes" if run.get("strikeouts_run") else "No")
    cols[3].metric("Validation", run.get("validation_status") or "—")

    picks = st.columns(4)
    picks[0].write(f"**Top HR:** {run.get('top_hr_pick') or '—'}")
    picks[1].write(f"**Top Hit:** {run.get('top_hit_pick') or '—'}")
    picks[2].write(f"**Top RBI:** {run.get('top_rbi_pick') or '—'}")
    picks[3].write(f"**Top ML/K:** {run.get('top_ml_pick') or run.get('top_k_prop') or '—'}")


def main():
    st.set_page_config(page_title="HR Projections 26", layout="wide")
    st.title("HR Projections 26 Portal")
    st.caption("App version: v20 — raw row_data display rebuild")
    st.caption("Shows only the 10 requested dashboard tabs. Each tab reads the matching individual Supabase sheet and displays parsed row_data directly.")

    runs = fetch_runs()
    if not runs:
        st.warning("No prediction runs found.")
        return

    labels = [format_run_label(r) for r in runs]
    latest_idx = 0
    for i, r in enumerate(runs):
        if r.get("is_latest"):
            latest_idx = i
            break

    selected_label = st.selectbox(
        "Published run",
        labels,
        index=latest_idx,
        key="published_run_selector_v20",
    )
    run = runs[labels.index(selected_label)]
    run_id = run.get("run_id")

    render_metric_cards(run)
    st.divider()

    raw_rows = fetch_prediction_rows(run_id)
    flat_df = flatten_rows(raw_rows)

    if flat_df.empty:
        st.error("Rows were returned as empty after parsing. Open the raw row debug below.")
    available_sheets = sorted([str(x) for x in flat_df.get("Sheet", pd.Series(dtype=str)).dropna().unique()])

    with st.expander("Available workbook sheets", expanded=False):
        st.write(available_sheets)

    with st.expander("Debug: raw Supabase row status", expanded=False):
        st.write({
            "raw_rows_returned": len(raw_rows),
            "flattened_rows": len(flat_df),
            "flattened_columns": list(flat_df.columns) if not flat_df.empty else [],
        })
        st.caption("First raw prediction_rows object:")
        if raw_rows:
            st.json(raw_rows[0])
        st.caption("First 10 parsed/flattened rows:")
        if not flat_df.empty:
            st.dataframe(safe_display_df(flat_df.head(10)), width="stretch", hide_index=True)

    tabs = st.tabs(LOCKED_TABS)

    for tab_name, tab in zip(LOCKED_TABS, tabs):
        with tab:
            st.subheader(tab_name)

            matched_sheet = find_sheet(available_sheets, tab_name)

            if matched_sheet is None:
                st.warning(f"No matching sheet found for: {tab_name}")
                st.write("Available sheets:", available_sheets)
                continue

            if matched_sheet != tab_name:
                st.info(f"Visible tab: {tab_name} | Source sheet used: {matched_sheet}")
            else:
                st.caption(f"Source sheet: {matched_sheet}")

            df = flat_df[flat_df["Sheet"].astype(str).str.strip().str.casefold() == matched_sheet.strip().casefold()].copy()

            if df.empty:
                st.warning(f"Matched sheet exists, but no parsed rows were found for {matched_sheet}.")
                continue

            search = st.text_input(
                "Search this tab",
                key=f"search_v20_{tab_name}",
                placeholder="Optional search...",
            )
            if search:
                s = search.lower().strip()
                df = df[df.astype(str).apply(lambda col: col.str.lower().str.contains(s, na=False)).any(axis=1)]

            st.caption(f"Showing {len(df):,} rows from {matched_sheet}")
            st.dataframe(safe_display_df(df), width="stretch", hide_index=True, height=650)


if __name__ == "__main__":
    main()
