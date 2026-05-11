#!/home/saijayanthai/Documents/spark/envs/ml-env/bin/python3
"""
Hourly safety net.

Two checks, in order of severity:

    1. Today's realised loss > 3% of starting equity (or initial capital
       proxy from the trade-journal stakes) → trigger emergency_stop.sh.
       Hard kill: dry_run=true, cancel orders, alert, restart.

    2. Last-7-day annualised Sharpe < 0  → halve `tradable_balance_ratio`
       in config.json (with a floor of 0.05) and restart the bot.

Designed for cron, e.g.

    0 * * * * /home/<user>/Documents/trading-bot/scripts/auto_rollback.py \
        >> /home/<user>/Documents/trading-bot/user_data/logs/auto_rollback.log 2>&1

Idempotent: re-running with no new trades closes is a no-op (each action
records a marker in the state file, so consecutive crons don't double-fire).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "user_data" / "config.json"
STATE_DIR = Path(os.environ.get("HOME", "/tmp")) / ".trading-bot"
STATE_FILE = STATE_DIR / "auto_rollback.json"
EMERGENCY_STOP = ROOT / "scripts" / "emergency_stop.sh"
def _resolve_dsn() -> str:
    """URL-encode-safe DSN — copy of user_data/modules/db.py:_resolve_dsn."""
    from urllib.parse import quote_plus
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "tradebot")
    password = os.environ.get("POSTGRES_PASSWORD", "tradebot-change-me")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5434")
    db = os.environ.get("POSTGRES_DB", "tradebot")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


DSN = _resolve_dsn()

logger = logging.getLogger("auto_rollback")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"last_emergency_stop": None, "last_halving_for_window": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_emergency_stop": None, "last_halving_for_window": None}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def _query_trades(start_dt: datetime) -> list[dict]:
    try:
        with psycopg.connect(DSN, connect_timeout=5) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT closed_at, pnl, pnl_pct, stake "
                    "FROM trade_journal "
                    "WHERE closed_at IS NOT NULL AND closed_at >= %s "
                    "ORDER BY closed_at ASC",
                    (start_dt,),
                )
                return list(cur.fetchall())
    except psycopg.errors.UndefinedTable:
        # Schema not initialised yet (bot hasn't booted) — treat as empty.
        return []
    except Exception as exc:
        logger.debug("auto_rollback query failed: %s", exc)
        return []


def _starting_equity_proxy(rows: list[dict], default: float = 10_000.0) -> float:
    if not rows:
        return default
    stakes = [float(r.get("stake") or 0.0) for r in rows]
    if not stakes:
        return default
    avg_stake = sum(stakes) / len(stakes)
    # 1 trade ≈ 10% of equity; rough but stable when no equity series exists.
    return max(default, avg_stake * 10.0)


def daily_loss_pct(now_utc: datetime) -> tuple[float, int]:
    """Returns (loss_fraction, n_trades) for today's UTC day."""
    day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = _query_trades(day_start)
    if not rows:
        return 0.0, 0
    starting = _starting_equity_proxy(rows)
    pnl_quote = sum(float(r.get("pnl") or 0.0) for r in rows)
    return float(-pnl_quote / starting), len(rows)


def weekly_sharpe(now_utc: datetime) -> tuple[float, int, int]:
    """Returns (sharpe_annualised, n_days, n_trades) over the last 7 UTC days."""
    cutoff = now_utc - timedelta(days=7)
    rows = _query_trades(cutoff)
    if not rows:
        return 0.0, 0, 0
    by_day: dict[str, float] = {}
    for r in rows:
        ts = r.get("closed_at")
        if isinstance(ts, datetime):
            day = ts.astimezone(timezone.utc).strftime("%Y-%m-%d")
        else:
            day = (str(ts or ""))[:10] or "0000-00-00"
        by_day[day] = by_day.get(day, 0.0) + float(r.get("pnl_pct") or 0.0)
    daily = list(by_day.values())
    if len(daily) < 2:
        return 0.0, len(daily), len(rows)
    mean = sum(daily) / len(daily)
    var = sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)
    sd = math.sqrt(var)
    if sd <= 0.0:
        return 0.0, len(daily), len(rows)
    return float((mean / sd) * math.sqrt(365.0)), len(daily), len(rows)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _trigger_emergency_stop(reason: str) -> bool:
    if not EMERGENCY_STOP.exists():
        logger.error("emergency_stop.sh not found at %s", EMERGENCY_STOP)
        return False
    if not os.access(EMERGENCY_STOP, os.X_OK):
        logger.warning("emergency_stop.sh not executable; running via bash")
    try:
        subprocess.run(
            ["bash", str(EMERGENCY_STOP), reason],
            check=False, timeout=120,
        )
        return True
    except Exception as exc:
        logger.error("emergency_stop.sh invocation failed: %s", exc)
        return False


def _halve_ratio(floor: float = 0.05) -> tuple[bool, float, float]:
    """Halve tradable_balance_ratio in config.json; returns (changed, old, new)."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception as exc:
        logger.error("could not read config.json: %s", exc)
        return False, 0.0, 0.0
    old = float(cfg.get("tradable_balance_ratio", 0.99))
    new = max(floor, round(old / 2.0, 4))
    if new >= old - 1e-9:
        return False, old, new
    cfg["tradable_balance_ratio"] = new
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=4))
    tmp.replace(CONFIG_PATH)
    # Restart freqtrade so the new ratio takes effect
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"),
             "restart", "freqtrade"],
            check=False, timeout=60,
        )
    except Exception as exc:
        logger.warning("freqtrade restart failed: %s", exc)
    return True, old, new


def _slack_notify(level: str, summary: str, **fields) -> None:
    """Best-effort Slack notification — silent on failure."""
    try:
        sys.path.insert(0, str(ROOT / "user_data"))
        from modules.slack_alerts import SlackAlerter
        s = SlackAlerter.from_env()
        if not s.enabled:
            return
        if level == "critical":
            s.notify_risk_critical("auto_rollback", 1.0, 0.0)
        elif level == "warning":
            s.notify_risk_warning("auto_rollback", 0.5, 0.0)
        s.notify_error("auto_rollback", summary, context=fields)
    except Exception as exc:
        logger.debug("slack notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--daily-loss-limit", type=float, default=0.03,
                   help="Daily-loss fraction that triggers emergency stop (default 0.03 = 3%)")
    p.add_argument("--sharpe-floor", type=float, default=0.0,
                   help="Weekly Sharpe below this halves the ratio (default 0.0)")
    p.add_argument("--ratio-floor", type=float, default=0.05,
                   help="Don't halve below this floor")
    p.add_argument("--dry", action="store_true", help="Compute + log only; take no action")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    state = _load_state()
    today_key = now.strftime("%Y-%m-%d")
    week_key = now.strftime("%Y-W%V")

    daily_loss, daily_n = daily_loss_pct(now)
    sharpe, n_days, week_trades = weekly_sharpe(now)

    logger.info(
        "tick: daily_loss=%.2f%% (trades=%d) | weekly_sharpe=%.2f (days=%d, trades=%d)",
        daily_loss * 100, daily_n, sharpe, n_days, week_trades,
    )

    # ---- check 1: daily loss > limit -> emergency stop -----------------
    if daily_loss > args.daily_loss_limit:
        if state.get("last_emergency_stop") == today_key:
            logger.info("emergency stop already triggered today; skipping")
        elif args.dry:
            logger.warning(
                "[dry] would trigger emergency_stop: daily_loss=%.2f%% > %.2f%%",
                daily_loss * 100, args.daily_loss_limit * 100,
            )
        else:
            logger.warning(
                "TRIGGER emergency_stop: daily_loss=%.2f%% > limit %.2f%%",
                daily_loss * 100, args.daily_loss_limit * 100,
            )
            ok = _trigger_emergency_stop(
                f"auto_rollback: daily_loss={daily_loss:.2%} > {args.daily_loss_limit:.2%}"
            )
            state["last_emergency_stop"] = today_key
            _save_state(state)
            _slack_notify(
                "critical",
                f"auto_rollback fired emergency stop (daily_loss={daily_loss:.2%})",
                daily_loss_pct=f"{daily_loss:.4f}",
                trades_today=daily_n,
            )
            return 0 if ok else 2

    # ---- check 2: weekly Sharpe < floor and we have data -> halve ratio --
    if n_days >= 3 and sharpe < args.sharpe_floor:
        if state.get("last_halving_for_window") == week_key:
            logger.info("ratio already halved this ISO week; skipping")
        elif args.dry:
            logger.warning(
                "[dry] would halve ratio: weekly_sharpe=%.2f < %.2f",
                sharpe, args.sharpe_floor,
            )
        else:
            logger.warning(
                "TRIGGER halve_ratio: weekly_sharpe=%.2f < %.2f",
                sharpe, args.sharpe_floor,
            )
            changed, old, new = _halve_ratio(floor=args.ratio_floor)
            if changed:
                logger.warning("ratio halved: %.4f -> %.4f", old, new)
                state["last_halving_for_window"] = week_key
                _save_state(state)
                _slack_notify(
                    "warning",
                    f"auto_rollback halved tradable_balance_ratio "
                    f"({old:.2f} → {new:.2f}); weekly_sharpe={sharpe:.2f}",
                    weekly_sharpe=f"{sharpe:.4f}",
                    old_ratio=f"{old:.4f}",
                    new_ratio=f"{new:.4f}",
                )
            else:
                logger.info("ratio already at or below floor; no change")

    return 0


if __name__ == "__main__":
    sys.exit(main())
