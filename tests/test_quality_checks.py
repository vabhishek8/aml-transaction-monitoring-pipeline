import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quality_checks import run_quality_checks  # noqa: E402


def _valid_row(**overrides):
    row = {
        "transaction_id": "TXN000000000001",
        "customer_id": "CUST00001",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "amount_aud": 120.0,
        "txn_type": "card_purchase",
        "merchant_category": "groceries",
        "city": "Sydney",
        "country": "AU",
        "latitude": -33.8688,
        "longitude": 151.2093,
        "corridor_risk": "low",
        "customer_risk_tier": "low",
    }
    row.update(overrides)
    return row


def test_valid_dataframe_passes():
    df = pd.DataFrame([_valid_row(), _valid_row(transaction_id="TXN000000000002")])
    report = run_quality_checks(df)
    assert report.passed


def test_missing_column_fails():
    df = pd.DataFrame([_valid_row()]).drop(columns=["customer_risk_tier"])
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "schema" for i in report.errors)


def test_empty_dataframe_fails():
    df = pd.DataFrame(columns=list(_valid_row().keys()))
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "row_count" for i in report.errors)


def test_negative_amount_fails():
    df = pd.DataFrame([_valid_row(amount_aud=-50.0)])
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "range" for i in report.errors)


def test_absurd_amount_fails():
    df = pd.DataFrame([_valid_row(amount_aud=999_999_999.0)])
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "range" for i in report.errors)


def test_duplicate_transaction_id_fails():
    df = pd.DataFrame([_valid_row(), _valid_row()])
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "duplicates" for i in report.errors)


def test_invalid_txn_type_fails():
    df = pd.DataFrame([_valid_row(txn_type="teleport")])
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "domain" for i in report.errors)


def test_invalid_risk_tier_fails():
    df = pd.DataFrame([_valid_row(customer_risk_tier="extreme")])
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "domain" for i in report.errors)


def test_bad_latitude_fails():
    df = pd.DataFrame([_valid_row(latitude=200.0)])
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "range" for i in report.errors)


def test_null_customer_id_is_error():
    df = pd.DataFrame([_valid_row(customer_id=None)])
    report = run_quality_checks(df)
    assert not report.passed
    assert any(i.check == "nulls" and i.severity == "error" for i in report.errors)
