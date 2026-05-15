#!/usr/bin/env python3
"""
Nightly Reflector — post-mortem writer for closed trades.

Once per day after market close (21:30 ET via Hermes cron), this script:
  1. Reads the day's closed trades from Postgres ``trade_journal``.
  2. Computes alpha vs benchmark (BTC for crypto pairs, SPY for stocks).
  3. Calls Qwen3-30B-A3B-Instruct via Ollama through ``chat_structured``
     to write a 2-4 sentence post-mortem.
  4. Promotes the matching ``pending`` line in
     ``stocks/memory/decisions.md`` to its realised form via the sibling
     ``shark.memory.update_with_outcome`` helper.

Design constraints
------------------
- ALWAYS exits 0 on internal errors so the cron does not alarm; errors
  go to Slack via the project's notifier (best-effort).
- Defensive imports — sibling branches ``stage/9-pydantic-schemas`` and
  ``stage/11-13-reflection-log`` may not be merged when this lands. Each
  missing import emits a clear log line and exits 0.
- Idempotent — re-running on the same day skips trades that already have
  a non-pending decision-line in ``decisions.md``.
- Model name is read from ``stocks/shark/llm/model_tiers.json`` via
  ``chat_structured(tier="reflector")`` so swapping models needs no code
  change. Phase B (week 4) swap to a fine-tuned adapter is a JSON edit.

Usage
-----
::

    # Cron (yesterday's closes, default)
    python scripts/nightly_reflector.py

    # Backfill ALL closed trades regardless of date (one-off catch-up)
    python scripts/nightly_reflector.py --backfill

    # Compute + log only; no LLM calls, no decisions.md writes
    python scripts/nightly_reflector.py --dry

The system + user prompts are ported from TradingAgents (Apache-2.0,
https://github.com/TauricResearch/TradingAgents) — full attribution in
the constants block below.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

# psycopg is the only hard dependency outside the trading-bot codebase
try:
    import psycopg
    from psycopg.rows import dict_row
    _HAVE_PG = True
except Exception:  # pragma: no cover — only hit when psycopg missing
    psycopg = None       # type: ignore[assignment]
    dict_row = None      # type: ignore[assignment]
    _HAVE_PG = False


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
STOCKS_DIR = ROOT / "stocks"
MEMORY_DIR = STOCKS_DIR / "memory"
DECISIONS_PATH = MEMORY_DIR / "decisions.md"
LOG_DIR = MEMORY_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)

MODEL_TIERS_PATH = STOCKS_DIR / "shark" / "llm" / "model_tiers.json"

# Crypto pairs end in /USD or /USDT (Coinbase / Binance convention used
# elsewhere in the codebase). Anything else is treated as a stock ticker.
_CRYPTO_PAIR_RE = re.compile(r"/USDT?$", re.IGNORECASE)

# Detect "+1.5%" / "-3.2%" / "0.0%" inside the reflection text. Used by
# the deterministic alpha-cited check after the LLM returns.
_ALPHA_PCT_RE = re.compile(r"[+-]?\d+(?:\.\d+)?\s*%")

# ── Reflector prompt — verbatim (port from TradingAgents Apache-2.0) ──
# Source: https://github.com/TauricResearch/TradingAgents — reflective
# memory module. Adapted for our trade-ledger schema. Apache-2.0.
REFLECTOR_SYSTEM = (
    "You are a trading analyst reviewing your own past decision now that "
    "the outcome is known.\n\n"
    "Write exactly 2-4 sentences of plain prose answering, in this order:\n"
    "1. Was the directional call correct? Cite the alpha figure as +X.X% "
    "or -X.X%.\n"
    "2. Which part of the investment thesis held or failed?\n"
    "3. One concrete lesson to apply to the next similar analysis.\n\n"
    "Constraints:\n"
    "- 2-4 sentences. Not 1, not 5.\n"
    "- Plain prose. No bullet lists, no headers, no markdown.\n"
    "- Cite the alpha figure at least once with one decimal place.\n"
    "- Reference only tags and entities present in the trade ledger "
    "below. Do not invent strategies, regimes, or tickers."
)

REFLECTOR_USER_TEMPLATE = (
    "Trade ledger:\n"
    "- Ticker: {ticker}\n"
    "- Entry tag: {entry_tag}\n"
    "- Exit reason: {exit_reason}\n"
    "- Entry: ${entry_price} on {open_date}\n"
    "- Exit: ${exit_price} on {close_date}\n"
    "- Holding: {holding_days} days\n"
    "- P&L: {pnl_usd:+.2f} ({pnl_pct:+.2f}%)\n"
    "- Alpha vs {benchmark}: {alpha_pct:+.2f}%\n"
    "- Regime at entry: {regime_in}\n"
    "- Regime at exit: {regime_out}\n"
    "- Original thesis (if recorded): {thesis_or_NA}\n\n"
    "Write the reflection."
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("nightly_reflector")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


# ---------------------------------------------------------------------------
# DSN / DB helpers
# ---------------------------------------------------------------------------


def _resolve_dsn() -> str:
    """URL-encode-safe DSN — copy of scripts/auto_rollback.py:_resolve_dsn."""
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


def _query_closed_trades(target_date: date | None = None,
                          backfill: bool = False) -> list[dict[str, Any]]:
    """Pull closed trades from ``trade_journal``.

    Args:
        target_date: When set, returns rows where ``closed_at::date`` equals
            this date (interpreted as UTC).
        backfill:    When True, returns ALL rows where ``closed_at`` is set
            and ignores ``target_date``.
    """
    if not _HAVE_PG:
        logger.error("psycopg not installed; cannot query trade_journal")
        return []
    dsn = _resolve_dsn()
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if backfill:
                    cur.execute(
                        "SELECT * FROM trade_journal "
                        "WHERE closed_at IS NOT NULL "
                        "ORDER BY closed_at ASC"
                    )
                else:
                    if target_date is None:
                        target_date = (datetime.now(UTC).date()
                                        - timedelta(days=1))
                    cur.execute(
                        "SELECT * FROM trade_journal "
                        "WHERE closed_at IS NOT NULL "
                        "  AND closed_at::date = %s "
                        "ORDER BY closed_at ASC",
                        (target_date,),
                    )
                return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("trade_journal query failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Benchmark / alpha
# ---------------------------------------------------------------------------


def _is_crypto(pair: str) -> bool:
    return bool(_CRYPTO_PAIR_RE.search(pair or ""))


def _benchmark_for(pair: str) -> str:
    """Return ``BTC/USD`` for crypto pairs, ``SPY`` for stocks."""
    return "BTC/USD" if _is_crypto(pair) else "SPY"


def _benchmark_return_pct(benchmark: str,
                           opened_at: datetime,
                           closed_at: datetime) -> float | None:
    """Returns the benchmark's percent return over [opened_at, closed_at].

    Tries (in order):
        1. yfinance for SPY (daily close-to-close).
        2. yfinance for BTC-USD (24/7 — yfinance still serves daily bars).
        3. Returns None if data fetch fails — caller treats alpha as
           equal to raw P&L pct (graceful degrade).

    The cron environment may not have yfinance installed; in that case
    we log a single warning and return None. Importing inside the
    function keeps the script importable for unit tests that monkeypatch
    this entire helper.
    """
    if opened_at is None or closed_at is None:
        return None
    if closed_at <= opened_at:
        return 0.0
    try:
        import yfinance as yf  # type: ignore[import-not-found]
    except Exception as exc:
        logger.info("yfinance unavailable (%s); alpha will fall back to raw pnl_pct", exc)
        return None

    symbol_map = {
        "BTC/USD": "BTC-USD",
        "SPY":     "SPY",
    }
    symbol = symbol_map.get(benchmark, benchmark)
    # Pad by 1 day on each side so the daily bars cover the holding window
    start = (opened_at - timedelta(days=1)).date().isoformat()
    end = (closed_at + timedelta(days=1)).date().isoformat()
    try:
        df = yf.download(
            symbol, start=start, end=end,
            progress=False, auto_adjust=True, threads=False,
        )
        if df is None or df.empty or "Close" not in df.columns:
            return None
        first = float(df["Close"].iloc[0])
        last = float(df["Close"].iloc[-1])
        if first <= 0:
            return None
        return (last - first) / first * 100.0
    except Exception as exc:
        logger.warning("yfinance download for %s failed: %s", symbol, exc)
        return None


def _coerce_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Defensive sibling imports
# ---------------------------------------------------------------------------


def _import_chat_structured():
    """Returns ``(chat_structured, BaseModel, Field, model_validator)`` or
    raises ImportError with a clear actionable message."""
    sys.path.insert(0, str(STOCKS_DIR))
    try:
        from shark.llm.structured import chat_structured  # type: ignore
    except Exception as exc:
        raise ImportError(
            "shark.llm.structured.chat_structured unavailable — sibling "
            "branch stage/9-pydantic-schemas not merged into this branch "
            f"(import error: {exc}). Once that branch lands, re-run the "
            f"reflector. Nothing to do until then."
        ) from exc
    try:
        from pydantic import BaseModel, Field, model_validator  # type: ignore
    except Exception as exc:  # pragma: no cover — pydantic is in env
        raise ImportError(
            "pydantic v2 required (BaseModel/Field/model_validator)"
        ) from exc
    return chat_structured, BaseModel, Field, model_validator


def _import_memory():
    """Returns ``shark.memory.update_with_outcome`` or raises ImportError."""
    sys.path.insert(0, str(STOCKS_DIR))
    try:
        from shark.memory import update_with_outcome  # type: ignore
    except Exception as exc:
        raise ImportError(
            "shark.memory.update_with_outcome unavailable — sibling "
            "branch stage/11-13-reflection-log not merged into this "
            f"branch (import error: {exc}). decisions.md cannot be "
            f"updated until that branch lands."
        ) from exc
    return update_with_outcome


def _build_reflection_schema(BaseModel, Field, model_validator):
    """Late-binds the schema so the file is importable without pydantic."""

    class ReflectionOut(BaseModel):  # type: ignore[misc, valid-type]
        text: str = Field(..., min_length=80, max_length=600)
        alpha_cited: bool = Field(
            ...,
            description="True if the alpha figure appears in the text",
        )

        @model_validator(mode="after")  # type: ignore[misc]
        def _alpha_must_appear(self):
            if not self.alpha_cited:
                raise ValueError("reflection must cite the alpha figure")
            return self

    return ReflectionOut


# ---------------------------------------------------------------------------
# Slack notifier (best effort, never raises)
# ---------------------------------------------------------------------------


def _slack_notify(msg: str) -> None:
    url = (os.environ.get("SLACK_WEBHOOK_URL", "") or "").strip()
    if not url:
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": msg}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as exc:  # pragma: no cover
        logger.debug("slack notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Idempotency — has this trade already been reflected?
# ---------------------------------------------------------------------------


def _decisions_text() -> str:
    try:
        return DECISIONS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as exc:  # pragma: no cover
        logger.warning("could not read decisions.md: %s", exc)
        return ""


def _already_reflected(decisions: str, ticker: str, close_date: str) -> bool:
    """Return True if a non-pending decisions.md line exists for this ticker
    on the given close-date.

    The convention from sibling branch stage/11-13: each decision line
    begins with ``- [<status>] <YYYY-MM-DD> <TICKER>``. Status is
    ``pending`` until reflected, then becomes the realised tag (e.g.
    ``WIN``/``LOSS``/``CLOSED``/``ALPHA+`` etc.). Anything that is NOT
    ``pending`` for the same ticker + close-date counts as already done.
    """
    if not decisions:
        return False
    safe_ticker = re.escape(ticker)
    pattern = re.compile(
        rf"^-\s*\[\s*(?P<status>[^\]]+)\s*\]\s*{re.escape(close_date)}\s+{safe_ticker}\b",
        re.MULTILINE,
    )
    for m in pattern.finditer(decisions):
        if (m.group("status") or "").strip().lower() != "pending":
            return True
    return False


# ---------------------------------------------------------------------------
# Per-trade reflection
# ---------------------------------------------------------------------------


def _entry_tag(row: dict[str, Any]) -> str:
    """Pull a free-text entry tag from whatever the journal exposed.

    Schema has no dedicated ``entry_tag`` column, so fall back to the
    most informative free-text fields in priority order. Prompt is a
    string template — never returns None.
    """
    for key in ("entry_tag", "tag", "reasoning", "regime"):
        v = row.get(key)
        if v:
            return str(v)
    return "n/a"


def _thesis_or_na(row: dict[str, Any]) -> str:
    for key in ("thesis", "reasoning", "notes"):
        v = row.get(key)
        if v:
            return str(v)
    return "N/A"


def _regimes(row: dict[str, Any]) -> tuple[str, str]:
    """Best-effort (regime_in, regime_out). Schema only exposes a single
    ``regime`` column at entry time, so use it for both unless the row
    carries explicit pre/post fields."""
    regime_in = (row.get("regime_in") or row.get("regime") or "unknown")
    regime_out = (row.get("regime_out") or row.get("regime_at_exit")
                  or row.get("regime") or "unknown")
    return str(regime_in), str(regime_out)


def _alpha_present_in_text(text: str, alpha_pct: float) -> bool:
    """Did the LLM actually quote the alpha figure?

    True if any signed percent in the text rounds (1dp) to the alpha. We
    compare to 1 decimal because the prompt forces 1-decimal output.
    """
    target = round(alpha_pct, 1)
    for m in _ALPHA_PCT_RE.finditer(text or ""):
        try:
            v = float(m.group().replace("%", "").strip())
        except ValueError:
            continue
        if round(v, 1) == target:
            return True
    return False


def _build_user_prompt(row: dict[str, Any], alpha_pct: float,
                        benchmark: str) -> str:
    opened_at = _coerce_dt(row.get("opened_at"))
    closed_at = _coerce_dt(row.get("closed_at"))
    open_date = opened_at.date().isoformat() if opened_at else "n/a"
    close_date = closed_at.date().isoformat() if closed_at else "n/a"
    holding_days = 0
    if opened_at and closed_at:
        holding_days = max(0, (closed_at - opened_at).days)
    pnl_usd = float(row.get("pnl") or 0.0)
    pnl_pct = float(row.get("pnl_pct") or 0.0)
    regime_in, regime_out = _regimes(row)
    return REFLECTOR_USER_TEMPLATE.format(
        ticker=row.get("pair") or "?",
        entry_tag=_entry_tag(row),
        exit_reason=row.get("exit_reason") or "unknown",
        entry_price=row.get("entry_price") or 0.0,
        exit_price=row.get("exit_price") or 0.0,
        open_date=open_date,
        close_date=close_date,
        holding_days=holding_days,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        benchmark=benchmark,
        alpha_pct=alpha_pct,
        regime_in=regime_in,
        regime_out=regime_out,
        thesis_or_NA=_thesis_or_na(row),
    )


def _reflect_one(
    row: dict[str, Any],
    *,
    chat_structured,
    schema,
    update_with_outcome,
    dry: bool = False,
) -> tuple[bool, str]:
    """Reflect on a single closed trade. Returns ``(success, message)``."""
    pair = str(row.get("pair") or "")
    benchmark = _benchmark_for(pair)
    opened_at = _coerce_dt(row.get("opened_at"))
    closed_at = _coerce_dt(row.get("closed_at"))
    if opened_at is None or closed_at is None:
        return False, f"{pair}: missing opened_at/closed_at"

    bench_pct = _benchmark_return_pct(benchmark, opened_at, closed_at)
    pnl_pct = float(row.get("pnl_pct") or 0.0)
    if bench_pct is None:
        # Graceful degrade: alpha defined as raw P&L when benchmark fetch fails.
        alpha_pct = pnl_pct
        benchmark_label = f"{benchmark} (n/a → raw pnl)"
    else:
        alpha_pct = pnl_pct - bench_pct
        benchmark_label = benchmark

    holding_days = max(0, (closed_at - opened_at).days)
    user_prompt = _build_user_prompt(row, alpha_pct, benchmark_label)

    if dry:
        return True, f"{pair}: [dry] alpha={alpha_pct:+.2f}% holding={holding_days}d"

    # ── Call LLM with up-to-2 retries; verify alpha citation deterministically
    last_err: Exception | None = None
    text = ""
    for attempt in range(2):
        try:
            result = chat_structured(
                provider="ollama",
                tier="reflector",
                system=REFLECTOR_SYSTEM,
                user=user_prompt,
                schema=schema,
                max_retries=2,
            )
            text = (getattr(result, "text", "") or "").strip()
            if _alpha_present_in_text(text, alpha_pct):
                break
            # alpha_cited was True per model self-report but our regex
            # disagrees — retry with an explicit corrective hint.
            last_err = ValueError(
                f"model claimed alpha_cited=True but {alpha_pct:+.1f}% not found in text"
            )
            user_prompt = (
                f"{user_prompt}\n\n"
                f"IMPORTANT: your previous attempt did not include the alpha "
                f"figure {alpha_pct:+.1f}% verbatim. Include it exactly."
            )
        except Exception as exc:
            last_err = exc
            logger.warning("reflector LLM attempt %d failed for %s: %s",
                           attempt + 1, pair, exc)
    if not text or (last_err is not None and not _alpha_present_in_text(text, alpha_pct)):
        return False, f"{pair}: LLM failed/no-alpha after retries: {last_err}"

    close_date = closed_at.date().isoformat()
    try:
        update_with_outcome(
            date=close_date,
            ticker=pair,
            pnl_pct=pnl_pct,
            alpha_pct=alpha_pct,
            holding_days=holding_days,
            reflection=text,
        )
    except Exception as exc:
        return False, f"{pair}: decisions.md update failed: {exc}"
    return True, f"{pair}: alpha={alpha_pct:+.2f}% reflected"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description="Nightly Reflector — write post-mortems for closed trades.",
    )
    p.add_argument(
        "--backfill", action="store_true",
        help="Reflect on ALL closed trades (ignores --date). Not in cron.",
    )
    p.add_argument(
        "--date", type=str, default=None,
        help="Override target close-date (YYYY-MM-DD UTC). Defaults to yesterday.",
    )
    p.add_argument(
        "--dry", action="store_true",
        help="Compute alpha + log only; no LLM calls, no decisions.md writes.",
    )
    args = p.parse_args()

    target_date: date | None = None
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            logger.error("invalid --date %r (want YYYY-MM-DD)", args.date)
            return 0

    # ── Defensive sibling imports — log + exit 0 if either is missing ──
    chat_structured = schema = update_with_outcome = None
    if not args.dry:
        try:
            chat_structured, BaseModel, Field, model_validator = _import_chat_structured()
            schema = _build_reflection_schema(BaseModel, Field, model_validator)
        except ImportError as exc:
            logger.error("reflector: %s", exc)
            _slack_notify(f":warning: nightly_reflector: {exc}")
            return 0
        try:
            update_with_outcome = _import_memory()
        except ImportError as exc:
            logger.error("reflector: %s", exc)
            _slack_notify(f":warning: nightly_reflector: {exc}")
            return 0

    rows = _query_closed_trades(target_date=target_date, backfill=args.backfill)
    if not rows:
        logger.info("reflector: no closed trades found "
                    "(backfill=%s date=%s)", args.backfill,
                    target_date or "yesterday")
        return 0

    decisions = _decisions_text()
    processed = written = errored = skipped = 0
    err_summaries: list[str] = []
    for row in rows:
        processed += 1
        pair = str(row.get("pair") or "")
        closed_at = _coerce_dt(row.get("closed_at"))
        close_date = closed_at.date().isoformat() if closed_at else ""

        # ── Idempotency: already reflected? ──
        if not args.backfill and close_date and _already_reflected(
            decisions, pair, close_date,
        ):
            skipped += 1
            logger.info("reflector: skip %s @ %s — already reflected",
                        pair, close_date)
            continue

        if args.dry:
            ok, msg = _reflect_one(
                row, chat_structured=chat_structured,
                schema=schema, update_with_outcome=update_with_outcome,
                dry=True,
            )
        else:
            ok, msg = _reflect_one(
                row, chat_structured=chat_structured,
                schema=schema, update_with_outcome=update_with_outcome,
            )
        if ok:
            written += 1
            logger.info("reflector: %s", msg)
        else:
            errored += 1
            err_summaries.append(msg)
            logger.error("reflector: %s", msg)

    summary = (
        f"reflector: processed {processed} trades, {written} reflections "
        f"written, {skipped} skipped (idempotent), {errored} errors"
    )
    logger.info(summary)
    if errored:
        _slack_notify(
            ":warning: *[nightly_reflector]* "
            f"{summary}\n• " + "\n• ".join(err_summaries[:5])
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
