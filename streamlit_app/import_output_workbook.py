from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from dotenv import load_dotenv


# HR Projections 26 importer guardrail contract:
# - Never insert the full workbook into prediction_rows.
# - Never insert Daily Prediction Register / audit / archive / manifest / carryforward sheets.
# - Insert rows only from explicit public/display sheet whitelist below.
# - Upload only a sanitized display-only workbook to Supabase Storage by default.
# - Keep a local JSON audit of inserted/skipped/blocked sheets for operator review.

MODEL_VERSION = "HR Projections 26 v2/v3 + Recap-Driven Market Boards Portal v28"
DEFAULT_STORAGE_BUCKET = "hr-projections-outputs"
DEFAULT_BATCH_SIZE = 500

# Sheet names exported by Excel/other writers can contain hidden characters
# such as BOM, NBSP, zero-width spaces, or Unicode dashes. Normalize them
# before whitelist matching so display tabs are not skipped because of a
# formatting artifact in the workbook tab name.
_HIDDEN_SHEET_CHARS = {
    "\ufeff": "",
    "\u200b": "",
    "\u200c": "",
    "\u200d": "",
    "\u2060": "",
    "\u00a0": " ",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2212": "-",
}


@dataclass(frozen=True)
class DisplaySheetSpec:
    label: str
    aliases: tuple[str, ...]
    max_rows: int
    combine_aliases: bool = False


# Keep this aligned with streamlit_app/app.py DISPLAY_TABS.
# Supabase prediction_rows.sheet_name is written as the canonical label, not as the raw workbook alias.
DISPLAY_SHEETS: tuple[DisplaySheetSpec, ...] = (
    DisplaySheetSpec(
        label="Top 10 HR Predictions",
        aliases=(
            "Top 10 HR Predictions",
            "Top 10 HR Prediction",
            "Top 10 HR",
            "Top 10 HR Board",
            "Top 10 Home Run Predictions",
            "Top Ten HR Predictions",
        ),
        max_rows=25,
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Top 5 Hit Predictions",
        aliases=(
            "Top 5 Hit Predictions",
            "Top 5 Hits Predictions",
            "Top 5 Hit Picks",
            "Top 5 Hits",
            "Top Five Hit Predictions",
        ),
        max_rows=25,
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Top 5 RBI Predictions",
        aliases=(
            "Top 5 RBI Predictions",
            "Top 5 RBI Picks",
            "Top 5 RBIs",
            "Top Five RBI Predictions",
        ),
        max_rows=25,
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Top 5 Walk Predictions",
        aliases=(
            "Top 5 Walk Predictions",
            "Top 5 BB Predictions",
            "Top 5 Walk Picks",
            "Top 5 Walks",
            "Top Five Walk Predictions",
        ),
        max_rows=25,
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Top 5 K Props",
        aliases=(
            "Top 5 K Props",
            "Top 5 Strikeout Props",
            "Top 5 Pitcher K Props",
            "Top Five K Props",
        ),
        max_rows=25,
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Top 5 ML Plays",
        aliases=(
            "Top 5 ML Plays",
            "Top 5 Moneyline Plays",
            "Top 5 Moneyline Predictions",
            "Top Five ML Plays",
        ),
        max_rows=25,
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Parlay Builder Market Boards",
        aliases=(
            "Parlay Builder Market Boards",
            "Parlay Builder Board",
            "Parlay Builder",
            "Market Board Parlay Builder",
        ),
        max_rows=500,
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Recap-Driven Market Board Audit",
        aliases=(
            "Recap-Driven Market Board Audit",
            "Recap Driven Market Board Audit",
            "Market Board Audit",
        ),
        max_rows=100,
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Top 4 Market Candidates",
        aliases=("Top 4 Market Candidates",),
        max_rows=500,
    ),
    DisplaySheetSpec(
        label="Strong Market Signal Board",
        aliases=("Strong Market Signal Board",),
        max_rows=500,
    ),
    DisplaySheetSpec(
        label="Best Game HR Coverage",
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
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Top 3 HR by Game",
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
        combine_aliases=True,
    ),
    DisplaySheetSpec(
        label="Final Bet Card",
        aliases=("Final Bet Card",),
        max_rows=250,
    ),
    DisplaySheetSpec(
        label="Risk-Adjusted Parlays",
        aliases=(
            "Risk-Adjusted Parlays",
            "Risk Adjusted Parlays",
            "RiskAdjusted Parlays",
            "Riskadjusted Parlays",
        ),
        max_rows=500,
    ),
    DisplaySheetSpec(
        label="Moneyline Predictions",
        aliases=(
            "Moneyline Predictions",
            "Moneylines Predictions",
        ),
        max_rows=100,
    ),
    DisplaySheetSpec(
        label="Strikeout Props",
        aliases=(
            "Strikeout Props",
            "K Props",
            "Pitcher Strikeout Props",
        ),
        max_rows=150,
    ),
    DisplaySheetSpec(
        label="Longshots HR",
        aliases=(
            "Longshots HR",
            "Longshot HR",
            "Longshot HR Candidates",
            "Longshots HR Candidates",
            "Longshot HR Rankings",
            "Longshots HR Rankings",
            "Longshot HR Board",
            "Longshots HR Board",
            "Longshot Home Run",
            "Longshots Home Run",
            "Longshot Home Runs",
            "Longshots Home Runs",
        ),
        max_rows=500,
        combine_aliases=True,
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

# Fragment block is intentionally broader than the exact-name block.  These are not public display tabs.
BLOCKED_NAME_FRAGMENTS = (
    "core hr top 30",
    "core hr top thirty",
    "audit",
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


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "market": ("Market", "Prop Market", "Market Type", "Bet Market"),
    "name": (
        "Name",
        "Player",
        "Player Name",
        "Name / Side",
        "Name/Side",
        "Selection",
        "Market Board Selection",
        "Recommended Pick",
        "Top Candidate",
        "Parlay Candidate",
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
    ),
    "team": ("Team", "Player Team", "Batter Team", "Pitcher Team"),
    "opponent": ("Opponent", "Opp", "Vs", "Against"),
    "game": ("Game", "Matchup", "Game Matchup"),
    "game_pk": ("GamePk", "GamePK", "gamePk", "Game ID", "GameId"),
    "raw_projection_rank": ("Raw Projection Rank", "Raw HR Rank", "Projection Rank", "Rank"),
    "projection_percent": (
        "Projection %",
        "Market Projection %",
        "Projected %",
        "Probability %",
        "Model Probability",
        "Win Probability",
        "True Win Probability",
        "HR Probability",
        "Hit Probability",
        "RBI Probability",
    ),
    "model_score": ("Model Score", "Market Model Score", "Raw Model Score", "Score"),
    "confidence_tier": ("Confidence Tier", "Market Confidence Tier", "Tier"),
    "recommended_usage": ("Recommended Usage", "Usage", "Recommendation", "Parlay Usage", "Board Usage", "Recommended Parlay Usage"),
    "final_bet_card_decision": ("Final Bet Card Decision", "Decision", "Bet Card Decision"),
    "should_bet": ("Should Bet?", "Should Bet", "Playable", "Play?"),
    "caution_flag": ("Caution Flag", "Risk Flag", "Flag"),
    "validation_status": ("Validation Status", "Status"),
}


def normalize_sheet_text(value: Any) -> str:
    text = "" if value is None else str(value)
    for src, dst in _HIDDEN_SHEET_CHARS.items():
        text = text.replace(src, dst)
    text = " ".join(text.strip().split())
    return text


def norm_text(value: Any) -> str:
    return normalize_sheet_text(value).casefold()


def norm_key(value: Any) -> str:
    # Robust matching for hyphen/spacing/case/hidden-character variants.
    return re.sub(r"[^a-z0-9]+", "", norm_text(value))


def is_blocked_sheet_name(sheet_name: str) -> bool:
    text = norm_text(sheet_name)
    compact = norm_key(sheet_name)
    blocked_exact = {norm_text(x) for x in BLOCKED_SHEETS} | {norm_key(x) for x in BLOCKED_SHEETS}
    if text in blocked_exact or compact in blocked_exact:
        return True
    return any(fragment in text for fragment in BLOCKED_NAME_FRAGMENTS)


def build_alias_lookup() -> dict[str, DisplaySheetSpec]:
    lookup: dict[str, DisplaySheetSpec] = {}
    for spec in DISPLAY_SHEETS:
        for alias in (spec.label, *spec.aliases):
            lookup[norm_key(alias)] = spec
    return lookup


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def excel_safe_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def make_unique_columns(columns: Iterable[Any]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in columns:
        base = str(raw).strip() if str(raw).strip() else "Unnamed"
        count = seen.get(base, 0)
        out.append(base if count == 0 else f"{base}.{count}")
        seen[base] = count + 1
    return out


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = make_unique_columns(df.columns)
    df = df.dropna(how="all")
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(lambda v: v.strip() if isinstance(v, str) else v)
    return df


def read_sheet(workbook: Path, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(workbook, sheet_name=sheet_name, engine="openpyxl")
    return clean_dataframe(df)


def find_field(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    # Exact first, normalized second.
    for alias in aliases:
        if alias in row and excel_safe_value(row.get(alias)) is not None:
            return excel_safe_value(row.get(alias))
    by_key = {norm_key(k): k for k in row.keys()}
    for alias in aliases:
        key = by_key.get(norm_key(alias))
        if key is not None and excel_safe_value(row.get(key)) is not None:
            return excel_safe_value(row.get(key))
    return None


def numeric_or_none(value: Any) -> float | None:
    value = excel_safe_value(value)
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def text_or_none(value: Any) -> str | None:
    value = excel_safe_value(value)
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def row_to_json(row: pd.Series, source_sheet: str, canonical_sheet: str) -> dict[str, Any]:
    data = {str(k): excel_safe_value(v) for k, v in row.to_dict().items()}
    data["_source_sheet"] = source_sheet
    data["_display_sheet"] = canonical_sheet
    return data


def make_prediction_row(run_id: str, canonical_sheet: str, source_sheet: str, excel_row_number: int, row: pd.Series) -> dict[str, Any]:
    row_dict = row_to_json(row, source_sheet=source_sheet, canonical_sheet=canonical_sheet)
    return {
        "run_id": run_id,
        "sheet_name": canonical_sheet,
        "row_number": excel_row_number,
        "market": text_or_none(find_field(row_dict, FIELD_ALIASES["market"])),
        "name": text_or_none(find_field(row_dict, FIELD_ALIASES["name"])),
        "team": text_or_none(find_field(row_dict, FIELD_ALIASES["team"])),
        "opponent": text_or_none(find_field(row_dict, FIELD_ALIASES["opponent"])),
        "game": text_or_none(find_field(row_dict, FIELD_ALIASES["game"])),
        "game_pk": text_or_none(find_field(row_dict, FIELD_ALIASES["game_pk"])),
        "raw_projection_rank": numeric_or_none(find_field(row_dict, FIELD_ALIASES["raw_projection_rank"])),
        "projection_percent": numeric_or_none(find_field(row_dict, FIELD_ALIASES["projection_percent"])),
        "model_score": numeric_or_none(find_field(row_dict, FIELD_ALIASES["model_score"])),
        "confidence_tier": text_or_none(find_field(row_dict, FIELD_ALIASES["confidence_tier"])),
        "recommended_usage": text_or_none(find_field(row_dict, FIELD_ALIASES["recommended_usage"])),
        "final_bet_card_decision": text_or_none(find_field(row_dict, FIELD_ALIASES["final_bet_card_decision"])),
        "should_bet": text_or_none(find_field(row_dict, FIELD_ALIASES["should_bet"])),
        "caution_flag": text_or_none(find_field(row_dict, FIELD_ALIASES["caution_flag"])),
        "validation_status": text_or_none(find_field(row_dict, FIELD_ALIASES["validation_status"])),
        "row_data": row_dict,
    }


def classify_workbook_sheets(workbook: Path) -> dict[str, Any]:
    xls = pd.ExcelFile(workbook, engine="openpyxl")
    alias_lookup = build_alias_lookup()
    allowed_by_label: dict[str, list[str]] = {spec.label: [] for spec in DISPLAY_SHEETS}
    blocked: list[str] = []
    skipped: list[str] = []
    classification_rows: list[dict[str, Any]] = []

    for raw_sheet in xls.sheet_names:
        sheet = normalize_sheet_text(raw_sheet)
        sheet_key = norm_key(sheet)

        # Whitelist exact normalized aliases before broad fragment blocking.
        # This prevents a future public display tab from being accidentally
        # blocked by a broad fragment rule. Non-whitelisted audit/register sheets
        # remain blocked below.
        spec = alias_lookup.get(sheet_key)
        if spec is not None:
            allowed_by_label[spec.label].append(raw_sheet)
            classification_rows.append({
                "Sheet": raw_sheet,
                "Normalized Sheet": sheet,
                "Normalized Key": sheet_key,
                "Classification": "Allowed Display",
                "Canonical Display Sheet": spec.label,
                "Reason": "Matched display whitelist alias",
            })
            continue

        if is_blocked_sheet_name(sheet):
            blocked.append(raw_sheet)
            classification_rows.append({
                "Sheet": raw_sheet,
                "Normalized Sheet": sheet,
                "Normalized Key": sheet_key,
                "Classification": "Blocked",
                "Canonical Display Sheet": "",
                "Reason": "Matched blocked sheet name or blocked fragment",
            })
            continue

        skipped.append(raw_sheet)
        classification_rows.append({
            "Sheet": raw_sheet,
            "Normalized Sheet": sheet,
            "Normalized Key": sheet_key,
            "Classification": "Skipped Non-Display",
            "Canonical Display Sheet": "",
            "Reason": "No display whitelist alias matched",
        })

    return {
        "sheet_names": xls.sheet_names,
        "allowed_by_label": allowed_by_label,
        "blocked_sheets": blocked,
        "skipped_non_display_sheets": skipped,
        "sheet_classification_rows": classification_rows,
        "display_alias_keys": sorted(alias_lookup.keys()),
    }


def build_display_frames(workbook: Path) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    classification = classify_workbook_sheets(workbook)
    display_frames: dict[str, pd.DataFrame] = {}
    inserted_source_sheets: dict[str, list[str]] = {}
    truncated: dict[str, int] = {}

    for spec in DISPLAY_SHEETS:
        matched = classification["allowed_by_label"].get(spec.label, [])
        if not matched:
            continue

        frames: list[pd.DataFrame] = []
        selected_sources: list[str] = []

        sources = matched if spec.combine_aliases else matched[:1]
        for source_sheet in sources:
            df = read_sheet(workbook, source_sheet)
            if df.empty:
                continue
            df.insert(0, "_source_sheet", source_sheet)
            df.insert(1, "_source_row_number", [int(i) + 2 for i in range(len(df))])
            frames.append(df)
            selected_sources.append(source_sheet)

        if not frames:
            continue

        combined = pd.concat(frames, ignore_index=True, sort=False)
        before_cap = len(combined)
        combined = combined.head(spec.max_rows).copy()
        display_frames[spec.label] = combined
        inserted_source_sheets[spec.label] = selected_sources
        if before_cap > spec.max_rows:
            truncated[spec.label] = before_cap - spec.max_rows

    audit = {
        **classification,
        "display_specs": [asdict(spec) for spec in DISPLAY_SHEETS],
        "inserted_display_sheets": list(display_frames.keys()),
        "inserted_source_sheets": inserted_source_sheets,
        "truncated_rows_by_display_sheet": truncated,
        "row_counts_by_display_sheet": {label: int(len(df)) for label, df in display_frames.items()},
        "total_rows_to_insert": int(sum(len(df) for df in display_frames.values())),
    }
    return display_frames, audit


def write_sanitized_workbook(display_frames: dict[str, pd.DataFrame], output_path: Path) -> None:
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for label, df in display_frames.items():
            # Excel sheet names cap at 31 chars.  All current labels are under the cap.
            export_df = df.drop(columns=["_source_sheet", "_source_row_number"], errors="ignore")
            export_df.to_excel(writer, index=False, sheet_name=label[:31])


def choose_first_name(rows: list[dict[str, Any]], market_tokens: tuple[str, ...]) -> str | None:
    for row in rows:
        market = norm_text(row.get("market") or row.get("sheet_name"))
        row_data = row.get("row_data") or {}
        all_text = " ".join([market, norm_text(row_data.get("Market")), norm_text(row_data.get("Market Type"))])
        if any(tok in all_text for tok in market_tokens):
            return text_or_none(row.get("name")) or text_or_none(row_data.get("Selection")) or text_or_none(row_data.get("Name / Side"))
    return None


def summarize_run(rows: list[dict[str, Any]], display_frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    games = {r.get("game") for r in rows if text_or_none(r.get("game"))}
    names = {r.get("name") for r in rows if text_or_none(r.get("name"))}
    validation_values = [r.get("validation_status") for r in rows if text_or_none(r.get("validation_status"))]

    return {
        "eligible_hitter_count": len(names) or None,
        "eligible_game_count": len(games) or None,
        "strikeouts_run": "Strikeout Props" in display_frames,
        "validation_status": "Display Import Guardrails Passed" if not validation_values else validation_values[0],
        "top_hr_pick": choose_first_name(rows, ("hr", "home run", "homer")),
        "top_hit_pick": choose_first_name(rows, ("hit", "hits")),
        "top_rbi_pick": choose_first_name(rows, ("rbi",)),
        "top_ml_pick": choose_first_name(rows, ("moneyline", "ml")),
        "top_k_prop": choose_first_name(rows, ("strikeout", " k ", "ks", "k prop")),
    }


def batched(items: list[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(items), batch_size):
        yield items[i : i + batch_size]


def get_env(name: str, required: bool = True, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def upload_display_workbook(supabase: Any, bucket: str, file_path: str, local_path: Path) -> None:
    with local_path.open("rb") as f:
        supabase.storage.from_(bucket).upload(
            file_path,
            f,
            file_options={
                "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "upsert": "true",
            },
        )


def publish_to_supabase(args: argparse.Namespace, display_frames: dict[str, pd.DataFrame], audit: dict[str, Any]) -> dict[str, Any]:
    load_dotenv()
    url = get_env("SUPABASE_URL")
    service_key = get_env("SUPABASE_SERVICE_ROLE_KEY")
    bucket = args.storage_bucket or get_env("SUPABASE_STORAGE_BUCKET", required=False, default=DEFAULT_STORAGE_BUCKET)
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError("The supabase package is required for a real publish. Run: pip install -r requirements.txt") from exc
    supabase = create_client(url, service_key)

    workbook_path = Path(args.workbook).resolve()
    source_hash = sha256_file(workbook_path)

    with tempfile.TemporaryDirectory(prefix="hrp26_display_import_") as tmpdir:
        tmp_path = Path(tmpdir) / f"{workbook_path.stem}__display_only.xlsx"
        write_sanitized_workbook(display_frames, tmp_path)
        sanitized_hash = sha256_file(tmp_path)

        run_payload = {
            "model_version": args.model_version,
            "slate_date": args.slate_date,
            "source_workbook_name": args.source_workbook_name,
            "output_workbook_name": tmp_path.name,
            "source_hash": source_hash,
            "output_hash": sanitized_hash,
            "is_latest": False,
            "notes": args.notes,
        }

        run_result = supabase.table("prediction_runs").insert(run_payload).execute()
        run_data = run_result.data or []
        if not run_data:
            raise RuntimeError("Supabase did not return a prediction_runs row after insert.")
        run_id = run_data[0]["run_id"]

        rows: list[dict[str, Any]] = []
        for canonical_sheet, df in display_frames.items():
            for index, row in df.iterrows():
                source_sheet = text_or_none(row.get("_source_sheet")) or canonical_sheet
                excel_row_number = int(row.get("_source_row_number") or (int(index) + 2))
                rows.append(make_prediction_row(run_id, canonical_sheet, source_sheet, excel_row_number, row.drop(labels=["_source_sheet", "_source_row_number"], errors="ignore")))

        try:
            for batch in batched(rows, args.batch_size):
                supabase.table("prediction_rows").insert(batch).execute()

            file_path = None
            if not args.skip_storage_upload:
                file_path = f"{args.slate_date or 'no-slate-date'}/{run_id}/{tmp_path.name}"
                upload_display_workbook(supabase, str(bucket), file_path, tmp_path)
                supabase.table("published_files").insert(
                    {
                        "run_id": run_id,
                        "file_type": "display_only_workbook",
                        "bucket_name": str(bucket),
                        "file_path": file_path,
                    }
                ).execute()

            summary = summarize_run(rows, display_frames)
            summary_notes = args.notes or ""
            guardrail_note = (
                f"Display-only import. Inserted sheets: {', '.join(audit['inserted_display_sheets'])}. "
                f"Blocked sheets: {len(audit['blocked_sheets'])}. Skipped non-display sheets: {len(audit['skipped_non_display_sheets'])}. "
                "Original full workbook was not uploaded to Storage."
            )
            update_payload = {
                **summary,
                "notes": (summary_notes + "\n" + guardrail_note).strip(),
            }
            supabase.table("prediction_runs").update(update_payload).eq("run_id", run_id).execute()

            # Set latest only after rows and sanitized upload succeed.
            supabase.table("prediction_runs").update({"is_latest": False}).neq("run_id", run_id).execute()
            supabase.table("prediction_runs").update({"is_latest": True}).eq("run_id", run_id).execute()

            return {
                "run_id": run_id,
                "rows_inserted": len(rows),
                "storage_bucket": str(bucket),
                "storage_file_path": file_path,
                "source_hash": source_hash,
                "display_workbook_hash": sanitized_hash,
            }
        except Exception:
            # Avoid leaving partial imported rows/runs visible if any insert/upload step fails.
            try:
                supabase.table("prediction_runs").delete().eq("run_id", run_id).execute()
            except Exception:
                pass
            raise


def write_audit(audit: dict[str, Any], args: argparse.Namespace, publish_result: dict[str, Any] | None = None) -> Path:
    workbook_path = Path(args.workbook).resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    default_path = workbook_path.with_name(f"{workbook_path.stem}__import_guardrail_audit__{timestamp}.json")
    audit_path = Path(args.audit_json).resolve() if args.audit_json else default_path
    audit_payload = {
        "generated_at_utc": timestamp,
        "workbook": str(workbook_path),
        "slate_date": args.slate_date,
        "dry_run": bool(args.dry_run),
        "guardrail_contract": {
            "prediction_rows_insert_policy": "display whitelist only",
            "blocked_upload_policy": "full workbook/register/audit/archive sheets are not inserted",
            "storage_policy": "sanitized display-only workbook only unless --skip-storage-upload is set",
        },
        "audit": audit,
        "publish_result": publish_result,
    }
    audit_path.write_text(json.dumps(audit_payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return audit_path


def print_sheet_diagnostics(audit: dict[str, Any]) -> None:
    print("Workbook sheet classification:")
    rows = audit.get("sheet_classification_rows") or []
    if not rows:
        print("  (No sheets found.)")
        return
    for row in rows:
        sheet = row.get("Sheet", "")
        cls = row.get("Classification", "")
        canon = row.get("Canonical Display Sheet", "")
        key = row.get("Normalized Key", "")
        if canon:
            print(f"  - {sheet} => {cls} as {canon} [key={key}]")
        else:
            print(f"  - {sheet} => {cls} [key={key}]")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import HR Projections 26 output workbook using display-sheet whitelist guardrails.")
    parser.add_argument("--workbook", required=True, help="Path to the HR Projections 26 output workbook.")
    parser.add_argument("--slate-date", required=True, help="Slate date in YYYY-MM-DD format.")
    parser.add_argument("--source-workbook-name", default=None, help="Name of the raw/source workbook used by the model.")
    parser.add_argument("--notes", default=None, help="Optional notes stored on prediction_runs.")
    parser.add_argument("--model-version", default=MODEL_VERSION)
    parser.add_argument("--storage-bucket", default=None, help="Supabase Storage bucket. Defaults to SUPABASE_STORAGE_BUCKET or hr-projections-outputs.")
    parser.add_argument("--skip-storage-upload", action="store_true", help="Insert DB rows but do not upload even the sanitized display workbook.")
    parser.add_argument("--dry-run", action="store_true", help="Prepare whitelist/block audit without connecting to Supabase.")
    parser.add_argument("--audit-json", default=None, help="Optional path for the local import guardrail audit JSON.")
    parser.add_argument("--list-sheets", action="store_true", help="Print workbook sheet classification and exit without Supabase access.")
    parser.add_argument("--debug-sheets", action="store_true", help="Print workbook sheet classification before import/dry-run.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    workbook_path = Path(args.workbook).expanduser().resolve()
    if not workbook_path.exists():
        raise FileNotFoundError(f"Workbook not found: {workbook_path}")
    if workbook_path.suffix.lower() not in {".xlsx", ".xlsm", ".xls"}:
        raise ValueError(f"Expected an Excel workbook, got: {workbook_path.name}")
    if args.batch_size <= 0:
        raise ValueError("--batch-size must be positive")

    display_frames, audit = build_display_frames(workbook_path)

    if args.list_sheets or args.debug_sheets:
        print_sheet_diagnostics(audit)
        print("Whitelisted display sheets detected:", ", ".join(audit.get("inserted_display_sheets", [])) or "None")
        if args.list_sheets:
            audit_path = write_audit(audit, args, publish_result=None)
            print(f"Guardrail audit written to: {audit_path}")
            return 0

    if audit["blocked_sheets"]:
        print(f"Blocked {len(audit['blocked_sheets'])} register/audit/archive sheet(s): {', '.join(audit['blocked_sheets'])}")
    if audit["skipped_non_display_sheets"]:
        print(f"Skipped {len(audit['skipped_non_display_sheets'])} non-display sheet(s): {', '.join(audit['skipped_non_display_sheets'])}")

    if not display_frames:
        audit_path = write_audit(audit, args, publish_result=None)
        print_sheet_diagnostics(audit)
        raise RuntimeError(
            "No whitelisted display sheets were found. Nothing was inserted or uploaded. "
            "Run with --list-sheets to inspect exact workbook tab names. "
            f"Audit written to: {audit_path}"
        )

    print("Whitelisted display sheets to insert:")
    for label, count in audit["row_counts_by_display_sheet"].items():
        sources = ", ".join(audit["inserted_source_sheets"].get(label, []))
        print(f"  - {label}: {count} row(s) from {sources}")
    print(f"Total rows to insert: {audit['total_rows_to_insert']}")

    publish_result = None
    if args.dry_run:
        print("Dry run only: Supabase insert/upload skipped.")
    else:
        publish_result = publish_to_supabase(args, display_frames, audit)
        print(f"Published run_id: {publish_result['run_id']}")
        print(f"Rows inserted: {publish_result['rows_inserted']}")
        if publish_result.get("storage_file_path"):
            print(f"Uploaded sanitized display workbook: {publish_result['storage_file_path']}")
        else:
            print("Storage upload skipped.")

    audit_path = write_audit(audit, args, publish_result=publish_result)
    print(f"Guardrail audit written to: {audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
