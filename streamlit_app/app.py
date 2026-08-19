from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st
from supabase import create_client


APP_VERSION = "v31_curated_primary_board_display"


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
    # 1. Complete HR ranking board
    DisplayTab(
        label="All HR Rankings",
        aliases=(
            "All HR Rankings",
            "All HR Ranking",
            "HR Rankings",
            "All Home Run Rankings",
        ),
        max_rows=500,
        combine_aliases=False,
    ),

    # 2. Best game coverage
    DisplayTab(
        label="Best Game Coverage",
        aliases=(
            "Best Game HR Coverage",
            "Best Game HR Coverage Board",
            "Game HR Coverage",
            "Game HR Coverage Board",
            "Best HR Game Coverage",
            "Best Game Coverage",
            "Best Game HR Candidates",
            "Game Attack HR Coverage",
        ),
        max_rows=500,
        combine_aliases=False,
    ),

    # 3. Top three HR candidates per game
    DisplayTab(
        label="Top 3 Per Game",
        aliases=(
            "Top 3 HR by Game",
            "Top 3 HR By Game",
            "Top 3 HR By Game Board",
            "Top 3 HR by Game Board",
            "Top 3 HR Candidates by Game",
            "Top 3 HR Candidates By Game",
            "Top 3 HR/Game",
            "Top 3 HR Per Game",
            "Top 3 Game HR",
            "Top 3 Game HR Candidates",
            "Top Three HR by Game",
            "Top Three HR By Game",
            "Top 3 Home Runs by Game",
            "Top 3 Home Run Candidates by Game",
        ),
        max_rows=500,
        combine_aliases=False,
    ),

    # 4. Primary/best two-leg HR board
    DisplayTab(
        label="Best 2-Leg Builder",
        aliases=(
            "HR 2-Leg Primary Pair Candidates",
            "HR 2 Leg Primary Pair Candidates",
            "HR 2-Leg Pair Builder",
            "HR 2 Leg Pair Builder",
            "2-Leg HR Builder",
            "2 Leg HR Builder",
            "2-Leg HR Pair Builder",
            "2 Leg HR Pair Builder",
            "HR 2-Leg Builder",
            "HR 2 Leg Builder",
            "Best 2-Leg HR Builder",
            "Best 2 Leg HR Builder",
            "Best 2-Leg Pair Builder",
            "Best 2 Leg Pair Builder",
        ),
        max_rows=500,
        combine_aliases=False,
    ),

    # 5. Secondary/value two-leg HR board
    DisplayTab(
        label="2-Leg Value Watchlist",
        aliases=(
            "HR 2-Leg Value Pair Watchlist",
            "HR 2 Leg Value Pair Watchlist",
            "HR 2-Leg Value Watchlist",
            "HR 2 Leg Value Watchlist",
            "2-Leg HR Value Watchlist",
            "2 Leg HR Value Watchlist",
            "2-Leg HR Value Pair Watchlist",
            "2 Leg HR Value Pair Watchlist",
            "2-Leg Value Pair Watchlist",
            "2 Leg Value Pair Watchlist",
            "Best 2-Leg Value Watchlist",
            "Best 2 Leg Value Watchlist",
            "Best 2-Leg HR Value Watchlist",
            "Best 2 Leg HR Value Watchlist",
        ),
        max_rows=500,
        combine_aliases=False,
    ),

    # 6. Top hits
    DisplayTab(
        label="Top Hits",
        aliases=(
            "Top 5 Hit Predictions",
            "Top 5 Hits Predictions",
            "Top 5 Hit Picks",
            "Top 5 Hits",
            "Top Five Hit Predictions",
        ),
        max_rows=25,
        combine_aliases=False,
    ),

    # 7. Top RBI
    DisplayTab(
        label="Top RBI",
        aliases=(
            "Top 5 RBI Predictions",
            "Top 5 RBI Picks",
            "Top 5 RBIs",
            "Top Five RBI Predictions",
        ),
        max_rows=25,
        combine_aliases=False,
    ),

    # 8. Top strikeouts
    DisplayTab(
        label="Top Strikeouts",
        aliases=(
            "Top 5 K Props",
            "Top 5 Strikeout Props",
            "Top 5 Pitcher K Props",
            "Top Five K Props",
            "K Actionable Picks",
        ),
        max_rows=25,
        combine_aliases=False,
    ),

    # 9. Top walks
    DisplayTab(
        label="Top Walks",
        aliases=(
            "Top 5 Walk Predictions",
            "Top 5 BB Predictions",
            "Top 5 Walk Picks",
            "Top 5 Walks",
            "Top Five Walk Predictions",
        ),
        max_rows=25,
        combine_aliases=False,
    ),

    # 10. Top moneylines
    DisplayTab(
        label="Top Moneylines",
        aliases=(
            "Top 5 ML Plays",
            "Top 5 Moneyline Plays",
            "Top 5 Moneyline Predictions",
            "Top Five ML Plays",
        ),
        max_rows=25,
        combine_aliases=False,
    ),

    # 11. New champion-picks card.  Prefer the newest multi-lane card first,
    # then fall back through historical champion-card names.
    DisplayTab(
        label="Champion Picks Card",
        aliases=(
            "HR Champion Multi-Lane Card",
            "Champion Picks Card",
            "HR Champion Picks Card",
            "Champion Picks",
            "Champion Card",
            "HR Confirmed Bet Card",
            "HR Top 10 Champion",
        ),
        max_rows=100,
        combine_aliases=False,
    ),
)

BLOCKED_SHEETS = {
    "Core HR Top 30",
    "Core HR Top 30 Board",
    "Core HR Rankings Top 30",
    "Core HR Top Thirty",
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
    "core hr top 30",
    "core hr top thirty",
    "register",
    "carryforward",
    "fallback",
    "source graph",
    "manifest",
    "validation",
    "archive",
    "full workbook",
    "raw workbook",
    "all rows",
    "score lock",
    "model lock",
    "canonical",
    "preflight",
    "recap source",
)


def _norm(value: Any) -> str:
    return str(value or "").strip().casefold()


def _is_blank(value: Any) -> bool:
    """True for values that should be treated as missing in display merging."""
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text in {"nan", "None", "<NA>", "NaT"}


def _is_blocked_sheet_name(sheet_name: str) -> bool:
    norm = _norm(sheet_name)
    if norm in {_norm(x) for x in BLOCKED_SHEETS}:
        return True
    return any(fragment in norm for fragment in BLOCKED_NAME_FRAGMENTS)


DYNAMIC_SHEET_HINTS = (
    "market board",
    "2-leg",
    "2 leg",
    "2leg",
    "two-leg",
    "two leg",
)


def _is_dynamic_display_sheet(sheet_name: str) -> bool:
    if not sheet_name or _is_blocked_sheet_name(sheet_name):
        return False
    norm = _norm(sheet_name)
    return any(hint in norm for hint in DYNAMIC_SHEET_HINTS)


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
def fetch_available_sheet_names(
    run_id: str,
    app_version: str = APP_VERSION,
    max_rows: int = 75000,
    page_size: int = 1000,
) -> list[str]:
    """Discover sheet names without fetching row_data/full workbook payloads.

    This is what makes v30 navigation truly dynamic. Only the sheet_name
    column is requested, and blocked sheets are never promoted into display
    navigation.
    """
    supabase = get_supabase_client()
    names: set[str] = set()
    start = 0

    while start < max_rows:
        end = min(start + page_size - 1, max_rows - 1)
        result = (
            supabase.table("prediction_rows")
            .select("sheet_name")
            .eq("run_id", run_id)
            .order("sheet_name")
            .range(start, end)
            .execute()
        )
        batch = result.data or []
        for row in batch:
            name = str(row.get("sheet_name") or "").strip()
            if name:
                names.add(name)
        if len(batch) < page_size:
            break
        start += page_size

    return sorted(names, key=lambda x: x.casefold())


def build_dynamic_specs(available_sheets: list[str]) -> list[DisplayTab]:
    """Create exact-sheet display specs for new market-board / 2-leg sheets.

    Anything already represented by a fixed alias is skipped to avoid
    duplicate tabs.
    """
    fixed_aliases = {
        _norm(alias)
        for spec in DISPLAY_TABS
        for alias in spec.aliases
    }
    dynamic: list[DisplayTab] = []
    for sheet in available_sheets:
        if _norm(sheet) in fixed_aliases:
            continue
        if not _is_dynamic_display_sheet(sheet):
            continue
        dynamic.append(
            DisplayTab(
                label=sheet,
                aliases=(sheet,),
                max_rows=500,
                combine_aliases=False,
            )
        )
    return dynamic


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
def fetch_dashboard_rows(run_id: str, app_version: str = APP_VERSION) -> dict[str, list[dict[str, Any]]]:
    """Return rows for display tabs only; never returns full archive/register data.

    app_version is intentionally part of the cache key so Streamlit refreshes
    displayed sheet aliases when the portal navigation contract changes.
    """
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
            # row_data may contain an explicitly blank display field such as
            # Market="". Treat blank as missing so the normalized Supabase
            # column can backfill it.
            if not _is_blank(val) and _is_blank(merged.get(dst)):
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
        "Candidate",
        "HR Candidate",
        "Best HR Candidate",
        "Best Game HR Candidate",
        "Top HR Candidate",
        "Top 3 HR Candidate",
        "Top 3 HR by Game Candidate",
        "Top 3 HR By Game Candidate",
        "Best Overall HR Candidate in Game",
        "HR Candidate 1",
        "HR Candidate 2",
        "HR Candidate 3",
        "Top HR 1",
        "Top HR 2",
        "Top HR 3",
        "Best RHB vs LHP",
        "Best LHB vs RHP",
        "Best RHB vs RHP Power",
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
        "Handedness Coverage Role",
        "Top HR Candidate",
        "Top 3 HR Candidate",
        "Top 3 HR by Game Candidate",
        "HR Candidate 1",
        "HR Candidate 2",
        "HR Candidate 3",
        "Top HR 1",
        "Top HR 2",
        "Top HR 3",
        "Best Overall HR Candidate in Game",
        "Best RHB vs LHP",
        "Best LHB vs RHP",
        "Best RHB vs RHP Power",
        "Coverage Roles Combined",
        "Game HR Cluster Score",
        "Rank Within Game",
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


def market_field_diagnostic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize where Market is coming from for troubleshooting."""
    db_nonblank = 0
    row_data_nonblank = 0
    total = len(rows)

    for row in rows:
        if not _is_blank(row.get("market")):
            db_nonblank += 1
        parsed = parse_row_data(row.get("row_data"))
        candidate = parsed.get("Market")
        if _is_blank(candidate):
            candidate = parsed.get("market")
        if not _is_blank(candidate):
            row_data_nonblank += 1

    displayed = flatten_rows(rows)
    displayed_nonblank = 0
    if not displayed.empty and "Market" in displayed.columns:
        displayed_nonblank = int((~displayed["Market"].map(_is_blank)).sum())

    return {
        "Rows": total,
        "DB market nonblank": db_nonblank,
        "row_data Market nonblank": row_data_nonblank,
        "Displayed Market nonblank": displayed_nonblank,
        "Displayed Market coverage %": round((displayed_nonblank / total * 100.0), 1) if total else 0.0,
    }


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


def render_tab(spec: DisplayTab, rows: list[dict[str, Any]], key_scope: str = "tab"):
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
        key=f"search_v31_{key_scope}_{spec.label}",
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
    st.success("Portal patch loaded: v31 — curated primary betting boards + Champion Picks Card")
    st.caption("Primary display: All HR Rankings, game coverage, top 3 per game, best 2-leg boards, top Hits/RBI/K/Walk/ML, and the Champion Picks Card. Audit/register/archive sheets remain blocked.")

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
        key="published_run_selector_v31",
    )
    run = runs[labels.index(selected_label)]
    run_id = run.get("run_id")

    render_metric_cards(run)
    st.divider()

    rows_by_tab = fetch_dashboard_rows(run_id, APP_VERSION)

    # v31 keeps the visible navigation deliberately curated. We still discover
    # workbook sheet names for diagnostics, but extra boards no longer clutter
    # the primary tab strip.
    available_sheets = fetch_available_sheet_names(run_id, APP_VERSION)
    all_specs = list(DISPLAY_TABS)

    with st.expander("Display/upload guardrail summary", expanded=False):
        summary_rows = []
        for spec in all_specs:
            rows = rows_by_tab.get(spec.label, [])
            summary_rows.append(
                {
                    "Visible Tab": spec.label,
                    "Aliases Checked": "; ".join(spec.aliases),
                    "Rows Loaded": len(rows),
                    "Row Cap": spec.max_rows,
                    "Source Sheets Returned": "; ".join(
                        sorted({str(r.get("sheet_name")) for r in rows if r.get("sheet_name")})
                    ),
                    "Guardrail Status": "Passed" if len(rows) <= spec.max_rows else "Manual Review",
                }
            )
        st.dataframe(
            safe_display_df(pd.DataFrame(summary_rows)),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "Only the curated primary betting boards are shown. "
            "Core HR Top 30, Daily Prediction Register, carryforward/register/audit/archive "
            "sheets remain blocked from public navigation."
        )

    with st.expander("Workbook sheet / source mapping diagnostic", expanded=False):
        mapped = []
        for spec in all_specs:
            rows = rows_by_tab.get(spec.label, [])
            mapped.append(
                {
                    "Portal Board": spec.label,
                    "Source Sheets": "; ".join(
                        sorted({str(r.get("sheet_name")) for r in rows if r.get("sheet_name")})
                    ),
                    "Rows": len(rows),
                }
            )

        champion_sheets = [
            s for s in available_sheets
            if "champion" in _norm(s) and not _is_blocked_sheet_name(s)
        ]
        two_leg_sheets = [
            s for s in available_sheets
            if any(h in _norm(s) for h in ("2-leg", "2 leg", "2leg", "two-leg", "two leg"))
            and not _is_blocked_sheet_name(s)
        ]

        st.dataframe(
            safe_display_df(pd.DataFrame(mapped)),
            width="stretch",
            hide_index=True,
        )
        st.write(
            {
                "available_sheet_count": len(available_sheets),
                "detected_champion_sheets": champion_sheets,
                "detected_two_leg_sheets": two_leg_sheets,
            }
        )

    with st.expander("Market field diagnostic", expanded=False):
        diag_rows = []
        for spec in all_specs:
            rows = rows_by_tab.get(spec.label, [])
            if not rows:
                continue
            if (
                "2-leg" in _norm(spec.label)
                or "moneyline" in _norm(spec.label)
                or "champion" in _norm(spec.label)
            ):
                diag_rows.append(
                    {"Board": spec.label, **market_field_diagnostic(rows)}
                )
        if diag_rows:
            st.dataframe(
                safe_display_df(pd.DataFrame(diag_rows)),
                width="stretch",
                hide_index=True,
            )
        else:
            st.caption("No relevant rows loaded for Market-field diagnostics.")

    # Keep only boards that actually returned rows. If the publisher has not
    # uploaded a requested board, it is listed in the missing-board notice
    # rather than replaced with unrelated dashboard tabs.
    visible_specs = [spec for spec in all_specs if rows_by_tab.get(spec.label)]
    missing_specs = [spec.label for spec in all_specs if not rows_by_tab.get(spec.label)]

    if missing_specs:
        st.caption(
            "Requested boards with no rows on this published run: "
            + ", ".join(missing_specs)
        )

    if not visible_specs:
        st.error("None of the curated primary boards returned rows for this run.")
        return

    # Quick access remains as a compact fallback when the tab strip overflows.
    selected_board = st.selectbox(
        "Quick access board",
        [spec.label for spec in visible_specs],
        index=0,
        key="quick_access_board_v31",
    )
    selected_spec = next(
        spec for spec in visible_specs if spec.label == selected_board
    )

    with st.expander(f"Quick access view — {selected_board}", expanded=False):
        render_tab(
            selected_spec,
            rows_by_tab.get(selected_spec.label, []),
            key_scope="quick",
        )

    tabs = st.tabs([spec.label for spec in visible_specs])
    for spec, tab in zip(visible_specs, tabs):
        with tab:
            render_tab(
                spec,
                rows_by_tab.get(spec.label, []),
                key_scope="tab",
            )


if __name__ == "__main__":
    main()