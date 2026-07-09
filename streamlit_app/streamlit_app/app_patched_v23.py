from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st
from supabase import create_client


# Streamlit/Supabase display contract:
# - Only fetch and render dashboard tabs that should be public/displayed.
# - Do not fetch full run history, Daily Prediction Register, or audit/archive sheets.
# - Apply hard per-tab row caps before data reaches Streamlit.

@dataclass(frozen=True)
class DisplayTab:
    label: str
    aliases: tuple[str, ...]
    max_rows: int
    combine_aliases: bool = False


DISPLAY_TABS: tuple[DisplayTab, ...] = (
    DisplayTab(
        label="Top 4 Market Candidates",
        aliases=("Top 4 Market Candidates",),
        max_rows=500,
    ),
    DisplayTab(
        label="Strong Market Signal Board",
        aliases=("Strong Market Signal Board",),
        max_rows=500,
    ),
    DisplayTab(
        label="Final Bet Card",
        aliases=("Final Bet Card",),
        max_rows=250,
    ),
    DisplayTab(
        label="Risk-Adjusted Parlays",
        aliases=(
            "Risk-Adjusted Parlays",
            "Risk Adjusted Parlays",
            "RiskAdjusted Parlays",
            "Riskadjusted Parlays",
        ),
        max_rows=500,
    ),
    DisplayTab(
        label="Moneyline Predictions",
        aliases=(
            "Moneyline Predictions",
            "Moneylines Predictions",
        ),
        max_rows=100,
    ),
    DisplayTab(
        label="Strikeout Props",
        aliases=(
            "Strikeout Props",
            "K Props",
            "Pitcher Strikeout Props",
        ),
        max_rows=150,
    ),
    DisplayTab(
        label="Longshot HR",
        aliases=(
            "Longshots HR",
            "Longshot HR",
            "Longshot HR Candidates",
            "Longshot HR Rankings",
            "Longshot HR Board",
            "Longshots HR Candidates",
        ),
        max_rows=500,
        combine_aliases=True,
    ),
)

BLOCKED_SHEETS = {
    "Daily Prediction Register",
    "Model Score Lock Results",
    "Prediction Register Export Audit",
    "Prediction Register Current Run Canonical Audit",
    "Prediction Register Canonical Audit",
    "Recap Source Audit",
    "Source Graph Audit",
    "Source Graph Detail",
    "Fallback Audit",
    "Preflight Audit",
    "Carryforward Audit",
    "Prediction Lock Carryforward Audit",
}

BLOCKED_NAME_FRAGMENTS = (
    "audit",
    "register",
    "carryforward",
    "fallback",
    "source graph",
    "manifest",
    "validation",
)


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


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
def fetch_sheet_rows(
    run_id: str,
    aliases: tuple[str, ...],
    max_rows: int,
    combine_aliases: bool = False,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    """Fetch only whitelisted sheet aliases for one visible tab.

    The old app fetched up to 50,000 prediction_rows for a run, which pulled
    archive/register rows into Streamlit. This function only queries the sheet
    names that are allowed for a visible tab and stops at the tab row cap.
    """
    supabase = get_supabase_client()
    all_rows: list[dict[str, Any]] = []
    blocked_norms = {_norm(x) for x in BLOCKED_SHEETS}

    for alias in aliases:
        if _norm(alias) in blocked_norms:
            continue

        alias_rows: list[dict[str, Any]] = []
        start = 0
        capped = max(0, int(max_rows) - len(all_rows)) if combine_aliases else int(max_rows)

        while start < capped:
            end = min(start + page_size - 1, capped - 1)
            result = (
                supabase.table("prediction_rows")
                .select("*")
                .eq("run_id", run_id)
                .eq("sheet_name", alias)
                .order("row_number")
                .range(start, end)
                .execute()
            )
            batch = result.data or []
            alias_rows.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size

        if alias_rows:
            all_rows.extend(alias_rows)
            if not combine_aliases:
                break

        if len(all_rows) >= int(max_rows):
            break

    return all_rows[: int(max_rows)]


@st.cache_data(ttl=30, show_spinner=False)
def fetch_dashboard_rows(run_id: str) -> dict[str, list[dict[str, Any]]]:
    """Return rows for display tabs only; never returns full archive/register data."""
    return {
        spec.label: fetch_sheet_rows(
            run_id=run_id,
            aliases=spec.aliases,
            max_rows=spec.max_rows,
            combine_aliases=spec.combine_aliases,
        )
        for spec in DISPLAY_TABS
    }


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

    # Hard safety: never display blocked archive/audit sheets, even if a bad alias is added later.
    if "Sheet" in df.columns:
        blocked_norms = {_norm(x) for x in BLOCKED_SHEETS}
        sheet_text = df["Sheet"].astype(str).map(_norm)
        blocked = sheet_text.isin(blocked_norms)
        for fragment in BLOCKED_NAME_FRAGMENTS:
            blocked = blocked | sheet_text.str.contains(fragment, regex=False, na=False)
        df = df[~blocked].copy()

    keep = []
    for col in df.columns:
        if col in {"Sheet", "Workbook Row #"}:
            keep.append(col)
            continue

        series = df[col].map(lambda v: "" if v is None or str(v) in {"nan", "None", "<NA>", "NaT"} else str(v).strip())
        if series.ne("").any():
            keep.append(col)

    df = df[keep]

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
        "GamePk",
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
    return df[ordered]


def safe_display_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out.columns = [str(c) for c in out.columns]

    seen: dict[str, int] = {}
    unique_cols = []
    for col in out.columns:
        count = seen.get(col, 0)
        unique_cols.append(col if count == 0 else f"{col}.{count}")
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


def render_tab(spec: DisplayTab, rows: list[dict[str, Any]]):
    st.subheader(spec.label)

    df = flatten_rows(rows)
    source_sheets = sorted({str(r.get("sheet_name")) for r in rows if r.get("sheet_name")})

    if df.empty:
        st.warning(f"No matching display rows found for: {spec.label}")
        st.caption("Checked sheet aliases: " + ", ".join(spec.aliases))
        return

    st.caption(
        f"Source sheet(s): {', '.join(source_sheets)} | "
        f"Row cap: {spec.max_rows:,} | Rows loaded: {len(df):,}"
    )

    search = st.text_input(
        "Search this tab",
        key=f"search_v23_{spec.label}",
        placeholder="Optional search...",
    )
    if search:
        s = search.lower().strip()
        df = df[df.astype(str).apply(lambda col: col.str.lower().str.contains(s, na=False)).any(axis=1)]

    st.caption(f"Showing {len(df):,} rows")
    st.dataframe(safe_display_df(df), width="stretch", hide_index=True, height=650)


def main():
    st.set_page_config(page_title="HR Projections 26", layout="wide")
    st.title("HR Projections 26 Portal")
    st.caption("App version: v23 — Display whitelist + Supabase read row caps")
    st.caption("Fetches only requested dashboard tabs. Daily Prediction Register and audit/archive sheets are not displayed or queried by default.")

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
        key="published_run_selector_v23",
    )
    run = runs[labels.index(selected_label)]
    run_id = run.get("run_id")

    render_metric_cards(run)
    st.divider()

    rows_by_tab = fetch_dashboard_rows(run_id)

    with st.expander("Display/upload guardrail summary", expanded=False):
        summary_rows = []
        for spec in DISPLAY_TABS:
            rows = rows_by_tab.get(spec.label, [])
            summary_rows.append(
                {
                    "Visible Tab": spec.label,
                    "Aliases Checked": "; ".join(spec.aliases),
                    "Rows Loaded": len(rows),
                    "Row Cap": spec.max_rows,
                    "Source Sheets Returned": "; ".join(sorted({str(r.get("sheet_name")) for r in rows if r.get("sheet_name")})),
                    "Guardrail Status": "Passed" if len(rows) <= spec.max_rows else "Manual Review",
                }
            )
        st.dataframe(safe_display_df(pd.DataFrame(summary_rows)), width="stretch", hide_index=True)
        st.caption("Blocked by default: Daily Prediction Register, register/carryforward/source-graph/audit/manifest/validation sheets.")

    tabs = st.tabs([spec.label for spec in DISPLAY_TABS])
    for spec, tab in zip(DISPLAY_TABS, tabs):
        with tab:
            render_tab(spec, rows_by_tab.get(spec.label, []))


if __name__ == "__main__":
    main()
