"""
Orchestrator: generate -> silver -> gold -> dashboard.

Run directly (`python src/pipeline.py`) or via the scheduled GitHub Actions
workflow. Each stage is independently importable/testable; this module just
sequences them and turns any stage failure into a non-zero exit code.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from datetime import datetime, timezone

from generate_transactions import generate_batch, write_bronze   # noqa: E402
from transform import load_latest_bronze, build_silver, write_silver  # noqa: E402
from gold import build_gold                                       # noqa: E402
from dashboard import render_dashboard                            # noqa: E402

logger = logging.getLogger("pipeline")


def run() -> None:
    root = Path(__file__).resolve().parents[1]
    bronze_dir = root / "data" / "bronze"
    silver_dir = root / "data" / "silver"
    gold_dir = root / "data" / "gold"
    dashboard_path = root / "docs" / "index.html"

    logger.info("stage 1/4: generate synthetic batch -> bronze")
    # Seeded by the current UTC date, not fixed: each scheduled run produces
    # a different (but reproducible-for-that-day) synthetic batch, so the
    # pipeline genuinely has new data to process each day rather than
    # replaying an identical fixture -- the same property a real nightly
    # core-banking extract has.
    daily_seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
    batch = generate_batch(seed=daily_seed)
    write_bronze(batch, bronze_dir)

    logger.info("stage 2/4: transform -> silver")
    bronze_df = load_latest_bronze(bronze_dir)
    silver_df = build_silver(bronze_df)
    silver_path = write_silver(silver_df, silver_dir)

    logger.info("stage 3/4: risk-score -> gold")
    gold_path, alerts_path, summary_path = build_gold(silver_path, gold_dir)

    logger.info("stage 4/4: render -> dashboard")
    render_dashboard(gold_path, alerts_path, summary_path, dashboard_path)

    logger.info("pipeline complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        run()
    except Exception:
        logger.exception("pipeline failed")
        sys.exit(1)
