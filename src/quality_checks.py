"""
Data quality gate for the silver layer -- the same fail-loudly contract used
across this portfolio: a failing check aborts the silver write rather than
letting bad rows reach the risk-scoring layer. In an AML context this isn't
an abstraction exercise -- a transaction-monitoring system that silently
drops or miscasts rows is a control failure a regulator will ask about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

REQUIRED_COLUMNS = {
    "transaction_id", "customer_id", "timestamp", "amount_aud", "txn_type",
    "merchant_category", "city", "country", "latitude", "longitude",
    "corridor_risk", "customer_risk_tier",
}

VALID_TXN_TYPES = {"card_purchase", "cash_deposit", "wire_in", "wire_out", "atm_withdrawal", "bank_transfer"}
VALID_RISK_TIERS = {"low", "medium", "high"}
AMOUNT_BOUNDS = (0.01, 250_000.0)  # a single retail transaction outside this is a parsing bug, not a whale


@dataclass
class QualityIssue:
    check: str
    severity: str
    message: str


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def add(self, check: str, severity: str, message: str) -> None:
        self.issues.append(QualityIssue(check, severity, message))

    def summary(self) -> str:
        lines = [f"Quality report: {len(self.errors)} error(s), {len(self.warnings)} warning(s)"]
        for i in self.issues:
            lines.append(f"  [{i.severity.upper()}] {i.check}: {i.message}")
        return "\n".join(lines)


def check_schema(df: pd.DataFrame, report: QualityReport) -> None:
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        report.add("schema", "error", f"missing required columns: {sorted(missing)}")


def check_not_empty(df: pd.DataFrame, report: QualityReport) -> None:
    if df.empty:
        report.add("row_count", "error", "dataframe has zero rows")


def check_nulls(df: pd.DataFrame, report: QualityReport) -> None:
    if df.empty:
        return
    critical = {"transaction_id", "customer_id", "timestamp", "amount_aud"}
    for col in REQUIRED_COLUMNS & set(df.columns):
        n_null = int(df[col].isna().sum())
        if n_null:
            severity = "error" if col in critical else "warning"
            report.add("nulls", severity, f"{col} has {n_null} null value(s)")


def check_amount_range(df: pd.DataFrame, report: QualityReport) -> None:
    if df.empty or "amount_aud" not in df.columns:
        return
    lo, hi = AMOUNT_BOUNDS
    bad = df[(df["amount_aud"] < lo) | (df["amount_aud"] > hi)]
    if not bad.empty:
        report.add("range", "error", f"amount_aud has {len(bad)} value(s) outside [{lo}, {hi}]")


def check_duplicate_transaction_id(df: pd.DataFrame, report: QualityReport) -> None:
    if df.empty or "transaction_id" not in df.columns:
        return
    dupes = df.duplicated(subset=["transaction_id"]).sum()
    if dupes:
        report.add("duplicates", "error", f"{dupes} duplicate transaction_id value(s)")


def check_valid_txn_type(df: pd.DataFrame, report: QualityReport) -> None:
    if df.empty or "txn_type" not in df.columns:
        return
    bad = set(df["txn_type"].dropna().unique()) - VALID_TXN_TYPES
    if bad:
        report.add("domain", "error", f"unrecognised txn_type value(s): {sorted(bad)}")


def check_valid_risk_tier(df: pd.DataFrame, report: QualityReport) -> None:
    if df.empty or "customer_risk_tier" not in df.columns:
        return
    bad = set(df["customer_risk_tier"].dropna().unique()) - VALID_RISK_TIERS
    if bad:
        report.add("domain", "error", f"unrecognised customer_risk_tier value(s): {sorted(bad)}")


def check_coordinates(df: pd.DataFrame, report: QualityReport) -> None:
    if df.empty or not {"latitude", "longitude"}.issubset(df.columns):
        return
    bad_lat = df[(df["latitude"] < -90) | (df["latitude"] > 90)]
    bad_lon = df[(df["longitude"] < -180) | (df["longitude"] > 180)]
    if not bad_lat.empty:
        report.add("range", "error", f"latitude has {len(bad_lat)} value(s) outside [-90, 90]")
    if not bad_lon.empty:
        report.add("range", "error", f"longitude has {len(bad_lon)} value(s) outside [-180, 180]")


CHECKS: list[Callable[[pd.DataFrame, QualityReport], None]] = [
    check_schema,
    check_not_empty,
    check_nulls,
    check_amount_range,
    check_duplicate_transaction_id,
    check_valid_txn_type,
    check_valid_risk_tier,
    check_coordinates,
]


def run_quality_checks(df: pd.DataFrame) -> QualityReport:
    report = QualityReport()
    for check in CHECKS:
        check(df, report)
    return report
