import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from transform import build_silver, write_silver  # noqa: E402


def _bronze_df():
    return pd.DataFrame([
        {
            "transaction_id": "TXN1", "customer_id": "CUST1",
            "timestamp": "2026-01-01T00:00:00+00:00", "amount_aud": 100.0,
            "txn_type": "card_purchase", "merchant_category": "groceries",
            "city": "Sydney", "country": "AU", "latitude": -33.8688, "longitude": 151.2093,
            "corridor_risk": "low", "customer_risk_tier": "low", "injected_typology": None,
        },
        {
            "transaction_id": "TXN1", "customer_id": "CUST1",  # exact duplicate id
            "timestamp": "2026-01-01T01:00:00.500000+00:00", "amount_aud": 100.0,
            "txn_type": "card_purchase", "merchant_category": "groceries",
            "city": "Sydney", "country": "AU", "latitude": -33.8688, "longitude": 151.2093,
            "corridor_risk": "low", "customer_risk_tier": "low", "injected_typology": None,
        },
        {
            "transaction_id": "TXN2", "customer_id": "CUST1",
            "timestamp": "2026-01-02T00:00:00+00:00", "amount_aud": 250.0,
            "txn_type": "bank_transfer", "merchant_category": "retail",
            "city": "Sydney", "country": "AU", "latitude": -33.8688, "longitude": 151.2093,
            "corridor_risk": "low", "customer_risk_tier": "low", "injected_typology": None,
        },
    ])


def test_build_silver_dedupes_and_handles_mixed_timestamp_precision():
    silver = build_silver(_bronze_df())
    assert len(silver) == 2
    assert silver["transaction_id"].is_unique
    assert pd.api.types.is_datetime64_any_dtype(silver["timestamp"])


def test_write_silver_rejects_out_of_range_amount(tmp_path):
    df = build_silver(_bronze_df())
    df.loc[0, "amount_aud"] = -10.0
    with pytest.raises(ValueError):
        write_silver(df, tmp_path)


def test_write_silver_succeeds_on_clean_data(tmp_path):
    df = build_silver(_bronze_df())
    out_path = write_silver(df, tmp_path)
    assert out_path.exists()
