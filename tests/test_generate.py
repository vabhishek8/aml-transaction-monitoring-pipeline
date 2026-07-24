import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from generate_transactions import generate_batch, CTR_THRESHOLD_AUD  # noqa: E402


def _small_batch(seed=1):
    return generate_batch(n_customers=120, n_normal=2500, days=60, seed=seed)


def test_generate_batch_has_expected_columns():
    df = _small_batch()
    expected = {
        "transaction_id", "customer_id", "timestamp", "amount_aud", "txn_type",
        "merchant_category", "city", "country", "latitude", "longitude",
        "corridor_risk", "customer_risk_tier", "injected_typology",
    }
    assert expected.issubset(set(df.columns))


def test_generate_batch_deterministic_with_seed():
    df1 = _small_batch(seed=42)
    df2 = _small_batch(seed=42)
    assert len(df1) == len(df2)
    assert df1["amount_aud"].sum() == df2["amount_aud"].sum()


def test_transaction_ids_are_unique():
    df = _small_batch()
    assert df["transaction_id"].is_unique


def test_all_amounts_positive():
    df = _small_batch()
    assert (df["amount_aud"] > 0).all()


def test_structuring_deposits_are_individually_under_ctr_threshold():
    df = _small_batch()
    structuring = df[df["injected_typology"] == "structuring"]
    assert len(structuring) > 0
    assert (structuring["amount_aud"] < CTR_THRESHOLD_AUD).all()


def test_layering_cases_are_balanced_in_out_pairs():
    df = _small_batch()
    layering = df[df["injected_typology"] == "layering"]
    assert len(layering) > 0
    assert (layering["txn_type"].value_counts().get("wire_in", 0) ==
            layering["txn_type"].value_counts().get("wire_out", 0))
