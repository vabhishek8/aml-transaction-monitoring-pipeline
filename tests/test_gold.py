"""
Detection-quality tests.

These validate the gold-layer SQL against the synthetic ground truth
(`injected_typology`) that src/gold.py itself never reads. Recall is
measured at the case level (did the scheme get an alert on ANY of its
transactions), not the row level -- a real transaction-monitoring system is
expected to flag a scheme once it has enough evidence, not retroactively
tag every leg of it, and asserting row-level recall would be testing the
wrong thing.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from generate_transactions import generate_batch  # noqa: E402
from transform import build_silver, write_silver  # noqa: E402
from gold import build_gold  # noqa: E402

TYPOLOGY_TO_FLAG = {
    "structuring": "flag_structuring",
    "layering": "flag_layering",
    "impossible_travel": "flag_impossible_travel",
    "amount_outlier": "flag_amount_outlier",
    "high_risk_corridor": "flag_high_risk_corridor",
}


@pytest.fixture(scope="module")
def scored(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gold_fixture")
    batch = generate_batch(n_customers=300, n_normal=8000, days=90, seed=7)
    silver = build_silver(batch)
    silver_path = write_silver(silver, tmp)
    gold_path, alerts_path, summary_path = build_gold(silver_path, tmp)
    return pd.read_parquet(gold_path), pd.read_parquet(alerts_path), pd.read_parquet(summary_path)


@pytest.mark.parametrize("typology,flag_col", list(TYPOLOGY_TO_FLAG.items()))
def test_case_level_recall_is_perfect_on_injected_typologies(scored, typology, flag_col):
    gold_df, _, _ = scored
    injected = gold_df[gold_df["injected_typology"] == typology]
    assert len(injected) > 0, f"fixture generated zero {typology} cases -- test is meaningless"
    case_hit_rate = injected.groupby("customer_id")[flag_col].any().mean()
    assert case_hit_rate == 1.0, f"{typology}: only caught {case_hit_rate:.0%} of injected cases"


def test_false_positive_rate_on_clean_transactions_is_low(scored):
    gold_df, _, _ = scored
    clean = gold_df[gold_df["injected_typology"].isna()]
    flag_cols = list(TYPOLOGY_TO_FLAG.values())
    any_flag = clean[flag_cols].any(axis=1)
    fp_rate = any_flag.mean()
    # high_risk_corridor legitimately fires on clean transactions that happen
    # to route through a high-risk jurisdiction/category -- that's a correct
    # flag, not a false positive, so a nonzero rate here is expected. This
    # test guards against a regression that makes the rules fire wildly.
    assert fp_rate < 0.12, f"false-positive rate too high: {fp_rate:.2%}"


def test_risk_score_bounded_0_to_100(scored):
    gold_df, _, _ = scored
    assert gold_df["risk_score"].min() >= 0
    assert gold_df["risk_score"].max() <= 100


def test_alert_queue_only_contains_scores_above_threshold(scored):
    _, alerts_df, _ = scored
    assert (alerts_df["risk_score"] >= 40).all()


def test_customer_summary_only_lists_customers_with_alerts(scored):
    _, _, summary_df = scored
    assert (summary_df["alert_count"] > 0).all()


def test_amount_outlier_uses_robust_stat_not_skewed_by_own_outlier(scored):
    """Regression guard: an earlier mean/stddev version of this check had its
    detection threshold dragged up by the very outlier it was measuring,
    which silently masked genuine outliers for low-transaction-count
    customers. This asserts recall stays perfect specifically for customers
    with few transactions, where that failure mode would resurface first."""
    gold_df, _, _ = scored
    counts = gold_df.groupby("customer_id").size()
    low_volume_customers = counts[counts <= 15].index
    injected = gold_df[
        (gold_df["injected_typology"] == "amount_outlier")
        & (gold_df["customer_id"].isin(low_volume_customers))
    ]
    if len(injected) == 0:
        pytest.skip("no low-volume customers received an injected outlier in this seed")
    assert injected["flag_amount_outlier"].all()
