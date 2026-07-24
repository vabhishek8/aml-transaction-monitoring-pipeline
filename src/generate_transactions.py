"""
Bronze layer: synthetic core-banking transaction feed.

No public dataset here -- real transaction data is, correctly, never public.
Instead this generates a statistically realistic batch of retail-banking
transactions and deliberately injects five AML typologies used in real
transaction-monitoring systems, so the gold-layer detection logic (src/gold.py)
has genuine positive cases to catch instead of a distribution with no signal
to find. This mirrors how AML engineering teams actually validate detection
logic before it ever sees production data: synthetic scenario injection with
a known ground truth.

Typologies injected (labelled in bronze via `injected_typology` for test/
validation purposes only -- gold-layer detection logic never reads this
column; it re-derives everything from the transaction fields alone, the same
constraint a real detection model has: no ground-truth label at inference
time).

  1. structuring   -- multiple cash deposits just under the AUD 10,000 /
                       USD 10,000 currency transaction reporting threshold,
                       placed within a short rolling window (AUSTRAC / FinCEN
                       CTR-threshold evasion pattern).
  2. layering       -- a large inbound credit followed within hours by an
                       outbound transfer of a similar amount ("pass-through"
                       / rapid fund movement, classic layering stage of
                       money laundering).
  3. impossible_travel -- two transactions by the same customer, in
                       geographically distant locations, closer together in
                       time than physical travel allows (card-present /
                       geo-velocity control).
  4. amount_outlier -- a transaction far outside a customer's own historical
                       amount distribution (statistical anomaly, not a fixed
                       threshold).
  5. high_risk_corridor -- transaction routed through a jurisdiction/merchant
                       category flagged as elevated AML risk.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

RNG_SEED = 20260724
CTR_THRESHOLD_AUD = 10_000.0

# Australian capitals + a few genuinely distant international cities, used
# both for "normal" transaction geography and to construct impossible-travel
# pairs with real, checkable great-circle distances.
CITIES = {
    "Sydney":    (-33.8688, 151.2093, "AU", "low"),
    "Melbourne": (-37.8136, 144.9631, "AU", "low"),
    "Brisbane":  (-27.4698, 153.0251, "AU", "low"),
    "Perth":     (-31.9523, 115.8613, "AU", "low"),
    "Adelaide":  (-34.9285, 138.6007, "AU", "low"),
    "Singapore": (1.3521, 103.8198, "SG", "medium"),
    "Dubai":     (25.2048, 55.2708, "AE", "high"),
    "London":    (51.5072, -0.1276, "GB", "low"),
    "Lagos":     (6.5244, 3.3792, "NG", "high"),
    "Vaduz":     (47.1410, 9.5209, "LI", "high"),
}

MERCHANT_CATEGORIES = [
    ("groceries", "low"), ("utilities", "low"), ("retail", "low"),
    ("dining", "low"), ("fuel", "low"), ("healthcare", "low"),
    ("cash_withdrawal", "medium"), ("wire_transfer", "medium"),
    ("crypto_exchange", "high"), ("money_service_business", "high"),
    ("online_gambling", "high"), ("precious_metals_dealer", "high"),
]

TXN_TYPES = ["card_purchase", "cash_deposit", "wire_in", "wire_out", "atm_withdrawal", "bank_transfer"]


@dataclass(frozen=True)
class Customer:
    customer_id: str
    home_city: str
    risk_tier: str          # "low" | "medium" | "high" -- KYC-assigned tier
    avg_txn_amount: float
    std_txn_amount: float


def _make_customers(n: int, rng: random.Random, np_rng: np.random.Generator) -> list[Customer]:
    customers = []
    cities = list(CITIES.keys())
    for i in range(n):
        home = rng.choice(cities[:5])  # customers are home-based in AU cities
        risk_tier = rng.choices(["low", "medium", "high"], weights=[0.82, 0.13, 0.05])[0]
        avg_amt = max(20.0, np_rng.normal(180, 90))
        std_amt = max(10.0, avg_amt * 0.35)
        customers.append(Customer(f"CUST{i:05d}", home, risk_tier, avg_amt, std_amt))
    return customers


def _random_timestamp(start: datetime, end: datetime, rng: random.Random) -> datetime:
    delta = end - start
    return start + timedelta(seconds=rng.randint(0, int(delta.total_seconds())))


def generate_normal_transactions(customers: list[Customer], start: datetime, end: datetime,
                                  n: int, rng: random.Random, np_rng: np.random.Generator) -> list[dict]:
    rows = []
    for _ in range(n):
        cust = rng.choice(customers)
        city = cust.home_city if rng.random() > 0.08 else rng.choice(list(CITIES.keys()))
        lat, lon, country, corridor_risk = CITIES[city]
        category, cat_risk = rng.choices(
            MERCHANT_CATEGORIES,
            weights=[10, 10, 10, 10, 10, 6, 5, 3, 1, 1, 1, 1],
        )[0]
        amount = max(5.0, np_rng.normal(cust.avg_txn_amount, cust.std_txn_amount))
        ts = _random_timestamp(start, end, rng)
        rows.append({
            "transaction_id": f"TXN{len(rows) + rng.randint(0, 10**9):012d}",
            "customer_id": cust.customer_id,
            "timestamp": ts.isoformat(),
            "amount_aud": round(amount, 2),
            "txn_type": rng.choice(TXN_TYPES),
            "merchant_category": category,
            "city": city,
            "country": country,
            "latitude": lat,
            "longitude": lon,
            "corridor_risk": corridor_risk,
            "customer_risk_tier": cust.risk_tier,
            "injected_typology": None,
        })
    return rows


def inject_structuring(customers: list[Customer], start: datetime, end: datetime,
                        n_cases: int, rng: random.Random) -> list[dict]:
    """Multiple sub-threshold cash deposits by the same customer within 48h."""
    rows = []
    for _ in range(n_cases):
        cust = rng.choice(customers)
        window_start = _random_timestamp(start, end - timedelta(days=2), rng)
        n_deposits = rng.randint(3, 5)
        remaining = CTR_THRESHOLD_AUD * rng.uniform(1.4, 2.2)
        for i in range(n_deposits):
            amt = min(remaining / (n_deposits - i), CTR_THRESHOLD_AUD * rng.uniform(0.78, 0.97))
            remaining -= amt
            ts = window_start + timedelta(hours=rng.uniform(0, 44))
            lat, lon, country, corridor_risk = CITIES[cust.home_city]
            rows.append({
                "transaction_id": f"TXN{rng.randint(0, 10**11):012d}",
                "customer_id": cust.customer_id,
                "timestamp": ts.isoformat(),
                "amount_aud": round(max(500.0, amt), 2),
                "txn_type": "cash_deposit",
                "merchant_category": "cash_withdrawal",
                "city": cust.home_city,
                "country": country,
                "latitude": lat,
                "longitude": lon,
                "corridor_risk": corridor_risk,
                "customer_risk_tier": cust.risk_tier,
                "injected_typology": "structuring",
            })
    return rows


def inject_layering(customers: list[Customer], start: datetime, end: datetime,
                     n_cases: int, rng: random.Random) -> list[dict]:
    """Large inbound wire followed within hours by an outbound transfer of a similar amount."""
    rows = []
    for _ in range(n_cases):
        cust = rng.choice(customers)
        ts_in = _random_timestamp(start, end - timedelta(hours=6), rng)
        amount = rng.uniform(8_000, 60_000)
        lat, lon, country, corridor_risk = CITIES[cust.home_city]
        rows.append({
            "transaction_id": f"TXN{rng.randint(0, 10**11):012d}",
            "customer_id": cust.customer_id, "timestamp": ts_in.isoformat(),
            "amount_aud": round(amount, 2), "txn_type": "wire_in",
            "merchant_category": "wire_transfer", "city": cust.home_city, "country": country,
            "latitude": lat, "longitude": lon, "corridor_risk": corridor_risk,
            "customer_risk_tier": cust.risk_tier, "injected_typology": "layering",
        })
        ts_out = ts_in + timedelta(hours=rng.uniform(0.5, 5))
        rows.append({
            "transaction_id": f"TXN{rng.randint(0, 10**11):012d}",
            "customer_id": cust.customer_id, "timestamp": ts_out.isoformat(),
            "amount_aud": round(amount * rng.uniform(0.85, 0.99), 2), "txn_type": "wire_out",
            "merchant_category": "wire_transfer", "city": cust.home_city, "country": country,
            "latitude": lat, "longitude": lon, "corridor_risk": corridor_risk,
            "customer_risk_tier": cust.risk_tier, "injected_typology": "layering",
        })
    return rows


def inject_impossible_travel(customers: list[Customer], start: datetime, end: datetime,
                              n_cases: int, rng: random.Random) -> list[dict]:
    """Two transactions, same customer, distant cities, implausibly close in time."""
    rows = []
    far_pairs = [("Sydney", "London"), ("Melbourne", "Dubai"), ("Perth", "Lagos"),
                 ("Brisbane", "Vaduz"), ("Adelaide", "Singapore")]
    for _ in range(n_cases):
        cust = rng.choice(customers)
        city_a, city_b = rng.choice(far_pairs)
        ts_a = _random_timestamp(start, end - timedelta(hours=2), rng)
        ts_b = ts_a + timedelta(minutes=rng.uniform(15, 90))  # too fast for the real distance
        for city, ts in ((city_a, ts_a), (city_b, ts_b)):
            lat, lon, country, corridor_risk = CITIES[city]
            rows.append({
                "transaction_id": f"TXN{rng.randint(0, 10**11):012d}",
                "customer_id": cust.customer_id, "timestamp": ts.isoformat(),
                "amount_aud": round(rng.uniform(40, 900), 2), "txn_type": "card_purchase",
                "merchant_category": "retail", "city": city, "country": country,
                "latitude": lat, "longitude": lon, "corridor_risk": corridor_risk,
                "customer_risk_tier": cust.risk_tier, "injected_typology": "impossible_travel",
            })
    return rows


def inject_amount_outliers(customers: list[Customer], start: datetime, end: datetime,
                            n_cases: int, rng: random.Random, np_rng: np.random.Generator) -> list[dict]:
    """A transaction far outside this specific customer's own historical amount pattern."""
    rows = []
    for _ in range(n_cases):
        cust = rng.choice(customers)
        amount = cust.avg_txn_amount + cust.std_txn_amount * rng.uniform(6, 11)
        ts = _random_timestamp(start, end, rng)
        lat, lon, country, corridor_risk = CITIES[cust.home_city]
        rows.append({
            "transaction_id": f"TXN{rng.randint(0, 10**11):012d}",
            "customer_id": cust.customer_id, "timestamp": ts.isoformat(),
            "amount_aud": round(amount, 2), "txn_type": rng.choice(["card_purchase", "bank_transfer"]),
            "merchant_category": rng.choice(["retail", "online_gambling", "precious_metals_dealer"]),
            "city": cust.home_city, "country": country, "latitude": lat, "longitude": lon,
            "corridor_risk": corridor_risk, "customer_risk_tier": cust.risk_tier,
            "injected_typology": "amount_outlier",
        })
    return rows


def inject_high_risk_corridor(customers: list[Customer], start: datetime, end: datetime,
                               n_cases: int, rng: random.Random) -> list[dict]:
    """Transaction through a high-risk jurisdiction / merchant category combination."""
    rows = []
    high_risk_cities = [c for c, v in CITIES.items() if v[3] == "high"]
    for _ in range(n_cases):
        cust = rng.choice(customers)
        city = rng.choice(high_risk_cities)
        lat, lon, country, corridor_risk = CITIES[city]
        category = rng.choice(["crypto_exchange", "money_service_business", "online_gambling"])
        ts = _random_timestamp(start, end, rng)
        rows.append({
            "transaction_id": f"TXN{rng.randint(0, 10**11):012d}",
            "customer_id": cust.customer_id, "timestamp": ts.isoformat(),
            "amount_aud": round(rng.uniform(2_000, 25_000), 2), "txn_type": "wire_out",
            "merchant_category": category, "city": city, "country": country,
            "latitude": lat, "longitude": lon, "corridor_risk": corridor_risk,
            "customer_risk_tier": cust.risk_tier, "injected_typology": "high_risk_corridor",
        })
    return rows


def generate_batch(
    n_customers: int = 600,
    n_normal: int = 18_000,
    days: int = 90,
    seed: int = RNG_SEED,
) -> pd.DataFrame:
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)

    customers = _make_customers(n_customers, rng, np_rng)

    rows = generate_normal_transactions(customers, start, end, n_normal, rng, np_rng)
    rows += inject_structuring(customers, start, end, max(6, n_normal // 1500), rng)
    rows += inject_layering(customers, start, end, max(6, n_normal // 1800), rng)
    rows += inject_impossible_travel(customers, start, end, max(6, n_normal // 1800), rng)
    rows += inject_amount_outliers(customers, start, end, max(8, n_normal // 1200), rng, np_rng)
    rows += inject_high_risk_corridor(customers, start, end, max(6, n_normal // 2000), rng)

    df = pd.DataFrame(rows)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def write_bronze(df: pd.DataFrame, bronze_dir: Path, run_ts: datetime | None = None) -> Path:
    run_ts = run_ts or datetime.now(timezone.utc)
    bronze_dir.mkdir(parents=True, exist_ok=True)
    out_path = bronze_dir / f"transactions_{run_ts.strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    with out_path.open("w") as f:
        for record in df.to_dict(orient="records"):
            f.write(json.dumps(record) + "\n")
    logger.info("wrote bronze batch: %s (%d rows)", out_path, len(df))
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[1]
    batch = generate_batch()
    written = write_bronze(batch, root / "data" / "bronze")
    print(f"generated {len(batch)} transactions -> {written}")
