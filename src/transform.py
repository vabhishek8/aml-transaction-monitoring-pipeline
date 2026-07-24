"""
Silver layer: parse bronze JSONL, validate through the quality gate, dedupe,
and persist a typed Parquet table.

`injected_typology` is carried through untouched from bronze. It exists
purely so tests/test_gold.py can measure detection recall/precision against
a known ground truth -- it is a labelling artefact of the synthetic
generator, not a real-world field, and src/gold.py's detection SQL never
reads it. A real detection system has no such column at inference time;
this pipeline is deliberately built to respect that constraint everywhere
except the test harness.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from quality_checks import run_quality_checks

logger = logging.getLogger(__name__)


def load_latest_bronze(bronze_dir: Path) -> pd.DataFrame:
    files = sorted(bronze_dir.glob("transactions_*.jsonl"))
    if not files:
        raise FileNotFoundError(f"no bronze files found in {bronze_dir}")
    latest = files[-1]
    records = [json.loads(line) for line in latest.read_text().splitlines() if line.strip()]
    df = pd.DataFrame.from_records(records)
    logger.info("loaded bronze batch: %s (%d rows)", latest, len(df))
    return df


def build_silver(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    df["amount_aud"] = pd.to_numeric(df["amount_aud"], errors="coerce")
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    df = df.drop_duplicates(subset=["transaction_id"], keep="first")
    df = df.sort_values(["customer_id", "timestamp"]).reset_index(drop=True)
    return df


def write_silver(df: pd.DataFrame, silver_dir: Path) -> Path:
    report = run_quality_checks(df)
    logger.info("\n%s", report.summary())
    if not report.passed:
        raise ValueError(
            f"silver write aborted -- {len(report.errors)} quality error(s):\n{report.summary()}"
        )

    silver_dir.mkdir(parents=True, exist_ok=True)
    out_path = silver_dir / "transactions.parquet"
    df.to_parquet(out_path, index=False)
    logger.info("wrote silver table: %s (%d rows)", out_path, len(df))
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[1]
    bronze_df = load_latest_bronze(root / "data" / "bronze")
    silver_df = build_silver(bronze_df)
    write_silver(silver_df, root / "data" / "silver")
