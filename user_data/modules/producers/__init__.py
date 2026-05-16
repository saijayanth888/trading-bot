"""
producers/ — single-source-of-truth data producers for the v5 dashboard.

Each module computes ONE category of operator-visible data, returns:
    {
        ... raw fields ...,
        "_meta": {
            "snapshot_ts":      ISO-8601 UTC,
            "age_s":            int | None,
            "stale":            bool,
            "market_open_now":  bool,
            "source":           "alpaca" | "trade_journal" | "wheel_state" | ...,
        },
    }

Consumers are the v5 routers under `user_data/dashboard/v5/`. Each router is
a thin FastAPI wrapper that calls a producer and returns raw data (no
envelope). Errors surface as RFC 7807 problem-detail responses.

Producers in this package:
  - portfolio.py     — equity + day P&L per side (B1)
  - metrics.py       — Sharpe + max-DD single truth (B3)
  - positions.py     — UNION crypto fills + wheel + shark (B6/B9)
  - shark_stats.py   — shark wins/losses backfill (B2)

Spec: docs/superpowers/specs/2026-05-16-trading-dashboard-redesign-design.md
"""
