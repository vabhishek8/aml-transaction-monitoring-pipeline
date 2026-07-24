"""
Renders the gold layer to a single static HTML file (Plotly, no server) --
a risk analyst's view of the scored batch: risk distribution, typology
breakdown, and the top of the alert queue (SAR-candidate list).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

logger = logging.getLogger(__name__)

FLAG_COLORS = {
    "Structuring": "#f2a541",
    "Layering": "#e8556f",
    "Impossible travel": "#4fd8c4",
    "Amount outlier": "#7c9cff",
    "High-risk corridor": "#b98ce8",
}


def render_dashboard(gold_path: Path, alerts_path: Path, summary_path: Path, out_path: Path) -> Path:
    gold = pd.read_parquet(gold_path)
    alerts = pd.read_parquet(alerts_path)
    summary = pd.read_parquet(summary_path)

    flag_counts = {
        "Structuring": int(gold["flag_structuring"].sum()),
        "Layering": int(gold["flag_layering"].sum()),
        "Impossible travel": int(gold["flag_impossible_travel"].sum()),
        "Amount outlier": int(gold["flag_amount_outlier"].sum()),
        "High-risk corridor": int(gold["flag_high_risk_corridor"].sum()),
    }

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=("Risk score distribution (all transactions)", "Flags raised, by typology"),
        specs=[[{"type": "xy"}, {"type": "xy"}]],
        column_widths=[0.55, 0.45],
    )
    fig.add_trace(
        go.Histogram(x=gold["risk_score"], nbinsx=30, marker_color="#f2a541",
                     name="risk_score"),
        row=1, col=1,
    )
    fig.add_vline(x=40, line_dash="dot", line_color="#e8556f", row=1, col=1,
                   annotation_text="alert threshold", annotation_font_color="#e8556f")

    fig.add_trace(
        go.Bar(
            x=list(flag_counts.keys()), y=list(flag_counts.values()),
            marker_color=[FLAG_COLORS[k] for k in flag_counts],
            name="flags",
        ),
        row=1, col=2,
    )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0a0e14", plot_bgcolor="#0a0e14",
        font=dict(family="Inter, system-ui, sans-serif", color="#c9d1d9", size=12),
        height=440, margin=dict(l=50, r=30, t=60, b=40), showlegend=False,
        title=dict(text="AML Transaction Monitoring -- Gold Layer", x=0.01, font=dict(size=20)),
    )
    fig.update_xaxes(gridcolor="rgba(255,255,255,0.06)")
    fig.update_yaxes(gridcolor="rgba(255,255,255,0.06)")

    chart_html = fig.to_html(full_html=False, include_plotlyjs="cdn", config={"displaylogo": False})

    alert_rows = "".join(
        f"<tr><td>{r.transaction_id}</td><td>{r.customer_id}</td>"
        f"<td>{pd.Timestamp(r.timestamp).strftime('%Y-%m-%d %H:%M')}</td>"
        f"<td>${r.amount_aud:,.0f}</td><td>{r.txn_type}</td>"
        f"<td>{r.city}</td><td><b>{r.risk_score}</b></td>"
        f"<td>{' '.join(t for t, v in [('Structuring', r.flag_structuring), ('Layering', r.flag_layering), ('Travel', r.flag_impossible_travel), ('Outlier', r.flag_amount_outlier), ('Corridor', r.flag_high_risk_corridor)] if v)}</td></tr>"
        for r in alerts.head(40).itertuples()
    )

    summary_rows = "".join(
        f"<tr><td>{r.customer_id}</td><td>{r.customer_risk_tier}</td><td>{r.total_txns}</td>"
        f"<td>${r.total_volume_aud:,.0f}</td><td>{r.avg_risk_score}</td>"
        f"<td>{r.max_risk_score}</td><td>{r.alert_count}</td></tr>"
        for r in summary.head(20).itertuples()
    )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AML Transaction Monitoring -- Gold Layer Dashboard</title>
<style>
  body {{ background:#0a0e14; color:#c9d1d9; font-family: Inter, system-ui, sans-serif; margin:0; padding:32px 24px 60px; }}
  h1 {{ font-size: 1.1rem; font-weight:600; color:#e6edf3; letter-spacing:.01em; margin:0 0 4px; }}
  h2 {{ font-size: 0.95rem; font-weight:600; color:#e6edf3; margin: 40px 0 12px; }}
  p.meta {{ color:#7d8590; font-size:.85rem; margin:0 0 28px; }}
  table {{ border-collapse: collapse; width:100%; margin-top:8px; font-size:.8rem; }}
  th, td {{ text-align:left; padding:7px 12px; border-bottom:1px solid rgba(255,255,255,0.08); white-space:nowrap; }}
  th {{ color:#7d8590; font-weight:500; text-transform:uppercase; font-size:.68rem; letter-spacing:.04em; }}
  tbody tr:hover {{ background: rgba(255,255,255,0.03); }}
  a {{ color:#4fd8c4; }}
  .scroll {{ overflow-x:auto; }}
</style>
</head>
<body>
  <h1>AML transaction monitoring -- synthetic batch, gold layer</h1>
  <p class="meta">Data: synthetic (src/generate_transactions.py) &middot; Generated {generated_at} by scheduled GitHub Actions run &middot;
     detection logic never reads ground-truth labels &middot; <a href="https://github.com/">source</a></p>
  {chart_html}

  <h2>Alert queue -- top 40 by risk score (score &ge; 40)</h2>
  <div class="scroll">
  <table>
    <thead><tr><th>Transaction</th><th>Customer</th><th>Timestamp</th><th>Amount</th>
    <th>Type</th><th>City</th><th>Score</th><th>Signals</th></tr></thead>
    <tbody>{alert_rows}</tbody>
  </table>
  </div>

  <h2>Highest-risk customers</h2>
  <div class="scroll">
  <table>
    <thead><tr><th>Customer</th><th>KYC tier</th><th>Txns</th><th>Volume</th>
    <th>Avg score</th><th>Max score</th><th>Alerts</th></tr></thead>
    <tbody>{summary_rows}</tbody>
  </table>
  </div>
</body>
</html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    logger.info("wrote dashboard: %s", out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[1]
    render_dashboard(
        root / "data" / "gold" / "transactions_scored.parquet",
        root / "data" / "gold" / "alert_queue.parquet",
        root / "data" / "gold" / "customer_summary.parquet",
        root / "docs" / "index.html",
    )
