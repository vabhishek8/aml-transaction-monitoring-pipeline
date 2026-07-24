"""
Gold layer: rule-based AML detection, in DuckDB SQL.

Every check here is derived only from transaction fields available at
inference time -- none of it reads `injected_typology` (that column exists
solely so tests/test_gold.py can score detection recall/precision against
the synthetic ground truth). This mirrors a real constraint: a production
transaction-monitoring system has no oracle label, only the behavioural
signal in the data itself.

Five independent signals, mirroring real AML typology detection:

  1. structuring        -- SQL window function, RANGE-framed by time (not
                            row count), summing sub-CTR-threshold cash
                            deposits per customer within a rolling 48h window.
  2. layering            -- self-join pairing a wire_in with a same-customer
                            wire_out of similar magnitude within 6 hours.
  3. impossible_travel   -- LAG window function to compare each transaction
                            against the same customer's immediately prior
                            transaction; haversine distance / elapsed hours
                            gives an implied travel speed, flagged if it
                            exceeds any plausible mode of transport.
  4. amount_outlier      -- per-customer z-score against that customer's own
                            historical mean/stddev (not a fixed dollar
                            threshold -- a $50 average customer and a $5,000
                            average customer need different bars).
  5. high_risk_corridor  -- jurisdiction/merchant-category risk flag.

A composite risk_score (0-100) blends all five; alerts (SAR-candidate queue)
are transactions crossing a configurable score threshold.
"""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

CTR_THRESHOLD_AUD = 10_000.0
STRUCTURING_WINDOW_SECONDS = 48 * 3600
LAYERING_WINDOW_HOURS = 6
LAYERING_AMOUNT_TOLERANCE = 0.20
IMPOSSIBLE_TRAVEL_KMH_THRESHOLD = 900.0  # above commercial flight cruise speed
AMOUNT_OUTLIER_Z_THRESHOLD = 5.0
ALERT_SCORE_THRESHOLD = 40

RISK_SCORE_SQL = f"""
with base as (
    select
        *,
        epoch(timestamp) as epoch_ts
    from silver
),

-- ---- 1. Structuring: rolling 48h sum/count of sub-threshold cash deposits ----
structuring as (
    select
        transaction_id,
        sum(case when txn_type = 'cash_deposit' and amount_aud < {CTR_THRESHOLD_AUD}
                 then amount_aud else 0 end)
            over (partition by customer_id order by epoch_ts
                  range between {STRUCTURING_WINDOW_SECONDS} preceding and current row) as deposit_sum_48h,
        sum(case when txn_type = 'cash_deposit' and amount_aud < {CTR_THRESHOLD_AUD}
                 then 1 else 0 end)
            over (partition by customer_id order by epoch_ts
                  range between {STRUCTURING_WINDOW_SECONDS} preceding and current row) as deposit_count_48h
    from base
),
structuring_flagged as (
    select transaction_id
    from structuring
    where deposit_count_48h >= 3 and deposit_sum_48h >= {CTR_THRESHOLD_AUD} * 0.9
),

-- ---- 2. Layering: wire_in paired with a similar-magnitude wire_out within 6h ----
layering_pairs as (
    select a.transaction_id as in_id, b.transaction_id as out_id
    from base a
    join base b
      on a.customer_id = b.customer_id
     and a.txn_type = 'wire_in'
     and b.txn_type = 'wire_out'
     and b.timestamp between a.timestamp and a.timestamp + interval '{LAYERING_WINDOW_HOURS} hours'
     and abs(b.amount_aud - a.amount_aud) / a.amount_aud <= {LAYERING_AMOUNT_TOLERANCE}
),
layering_flagged as (
    select in_id as transaction_id from layering_pairs
    union
    select out_id as transaction_id from layering_pairs
),

-- ---- 3. Impossible travel: implied speed vs same customer's prior transaction ----
with_prev as (
    select
        *,
        lag(latitude) over w as prev_lat,
        lag(longitude) over w as prev_lon,
        lag(timestamp) over w as prev_ts
    from base
    window w as (partition by customer_id order by timestamp)
),
travel_calc as (
    select
        transaction_id,
        prev_ts,
        timestamp,
        -- haversine distance in km
        2 * 6371 * asin(sqrt(
            pow(sin(radians(latitude - prev_lat) / 2), 2) +
            cos(radians(prev_lat)) * cos(radians(latitude)) *
            pow(sin(radians(longitude - prev_lon) / 2), 2)
        )) as distance_km,
        greatest(date_diff('second', prev_ts, timestamp) / 3600.0, 0.0166667) as elapsed_hours
    from with_prev
    where prev_ts is not null
),
impossible_travel_flagged as (
    select transaction_id
    from travel_calc
    where (distance_km / elapsed_hours) > {IMPOSSIBLE_TRAVEL_KMH_THRESHOLD}
      and distance_km > 200
),

-- ---- 4. Amount outlier: per-customer ROBUST z-score (median/MAD) ----
-- A plain mean/stddev z-score is exactly the wrong tool here: the outlier
-- transaction itself inflates the sample stddev it's being measured against
-- (worse for customers with few transactions), which masks the very thing
-- we're trying to detect. Median Absolute Deviation is robust to a small
-- number of extreme values contaminating the baseline, so the score isn't
-- self-defeating.
customer_median as (
    select customer_id, median(amount_aud) as cust_median
    from base
    group by customer_id
),
deviations as (
    select
        b.transaction_id,
        b.customer_id,
        b.amount_aud - cm.cust_median as signed_dev,
        abs(b.amount_aud - cm.cust_median) as abs_dev
    from base b
    join customer_median cm using (customer_id)
),
customer_mad as (
    select customer_id, median(abs_dev) as cust_mad
    from deviations
    group by customer_id
),
outlier_calc as (
    select
        d.transaction_id,
        -- 0.6745 scales MAD to be comparable to a normal-distribution stddev
        case when m.cust_mad is null or m.cust_mad < 1.0 then 0
             else 0.6745 * d.signed_dev / m.cust_mad end as amount_zscore
    from deviations d
    join customer_mad m using (customer_id)
),
amount_outlier_flagged as (
    select transaction_id from outlier_calc where amount_zscore > {AMOUNT_OUTLIER_Z_THRESHOLD}
),

-- ---- 5. High-risk corridor ----
high_risk_flagged as (
    select transaction_id
    from base
    where corridor_risk = 'high'
       or merchant_category in ('crypto_exchange', 'money_service_business', 'precious_metals_dealer')
)

select
    b.transaction_id,
    b.customer_id,
    b.timestamp,
    b.amount_aud,
    b.txn_type,
    b.merchant_category,
    b.city,
    b.country,
    b.corridor_risk,
    b.customer_risk_tier,
    b.injected_typology,
    round(coalesce(oc.amount_zscore, 0), 2) as amount_zscore,
    (sf.transaction_id is not null) as flag_structuring,
    (lf.transaction_id is not null) as flag_layering,
    (itf.transaction_id is not null) as flag_impossible_travel,
    (aof.transaction_id is not null) as flag_amount_outlier,
    (hrf.transaction_id is not null) as flag_high_risk_corridor,
    least(100,
        (sf.transaction_id is not null)::int * 30 +
        (lf.transaction_id is not null)::int * 30 +
        (itf.transaction_id is not null)::int * 35 +
        (aof.transaction_id is not null)::int * 15 +
        (hrf.transaction_id is not null)::int * 10 +
        case b.customer_risk_tier when 'high' then 10 when 'medium' then 4 else 0 end
    ) as risk_score
from base b
left join structuring_flagged sf using (transaction_id)
left join layering_flagged lf using (transaction_id)
left join impossible_travel_flagged itf using (transaction_id)
left join amount_outlier_flagged aof using (transaction_id)
left join outlier_calc oc using (transaction_id)
left join high_risk_flagged hrf using (transaction_id)
order by risk_score desc, b.timestamp
"""

ALERT_QUEUE_SQL = f"""
select *
from gold
where risk_score >= {ALERT_SCORE_THRESHOLD}
order by risk_score desc, timestamp
"""

CUSTOMER_SUMMARY_SQL = """
select
    customer_id,
    customer_risk_tier,
    count(*) as total_txns,
    round(sum(amount_aud), 2) as total_volume_aud,
    round(avg(risk_score), 1) as avg_risk_score,
    max(risk_score) as max_risk_score,
    sum(case when risk_score >= 40 then 1 else 0 end) as alert_count
from gold
group by customer_id, customer_risk_tier
having alert_count > 0
order by max_risk_score desc, alert_count desc
"""


def build_gold(silver_path: Path, gold_dir: Path):
    gold_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"create view silver as select * from read_parquet('{silver_path.as_posix()}')")

    gold_df = con.execute(RISK_SCORE_SQL).df()
    con.register("gold", gold_df)
    alerts_df = con.execute(ALERT_QUEUE_SQL).df()
    summary_df = con.execute(CUSTOMER_SUMMARY_SQL).df()

    gold_path = gold_dir / "transactions_scored.parquet"
    alerts_path = gold_dir / "alert_queue.parquet"
    summary_path = gold_dir / "customer_summary.parquet"
    gold_df.to_parquet(gold_path, index=False)
    alerts_df.to_parquet(alerts_path, index=False)
    summary_df.to_parquet(summary_path, index=False)

    logger.info("wrote gold table: %s (%d rows)", gold_path, len(gold_df))
    logger.info("wrote alert queue: %s (%d rows, threshold=%d)", alerts_path, len(alerts_df), ALERT_SCORE_THRESHOLD)
    logger.info("wrote customer summary: %s (%d rows)", summary_path, len(summary_df))
    con.close()
    return gold_path, alerts_path, summary_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[1]
    build_gold(root / "data" / "silver" / "transactions.parquet", root / "data" / "gold")
