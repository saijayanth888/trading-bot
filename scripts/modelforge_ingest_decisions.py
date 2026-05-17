#!/usr/bin/env python3
"""
modelforge_ingest_decisions — one-shot historical bootstrap ETL from
``quanta_schema.decisions`` for the 4 roles that can draw from crypto history.

Roles supported: bull, bear, arbiter, regime-tagger.
Roles NOT bootstrapped here: reflector (stock-only — must NOT use crypto),
indicator-selector (zero records; wire the agent first per Section I).

Source table schema
-------------------
``quanta_schema.decisions``: id, ts, symbol, strategy, debate JSONB,
outcome TEXT, rationale TEXT.

Per-row mapping
---------------
- **bull:** debate["bull"] text → agent="risk_debate.aggressive"
- **bear:** debate["bear"] text → agent="risk_debate.conservative"
- **arbiter:** outcome + rationale + debate summary → agent="risk_debate.neutral"
- **regime-tagger:** strategy → RegimeLabel mapping → agent="regime_tagger"

Crypto-term blocklist is applied at write time (defense-in-depth — also
applied by modelforge_curate.py at curate time).

Output format
-------------
JSONL rows written to ``<raw_root>/<role>/decisions_<low_id>_<high_id>.jsonl``.
Each row has the same schema as ``modelforge_ingest.py::llm_call_example()``:
{ts, ticker, system_message, user_message, response, pending_outcome=False,
outcome_key, ledger{agent, valid, ...}}.

This schema is what ``modelforge_curate.py::_iter_jsonl()`` expects when it
reads from ``~/.dgx-train/raw/<role>/*.jsonl``.

Exit codes
----------
0 — at least one role wrote at least one record.
1 — all roles produced 0 records (crypto blocklist killed everything,
    or no decisions rows for this role). Operator must check output.

CLI
---
    python scripts/modelforge_ingest_decisions.py --role all --limit 5000
    python scripts/modelforge_ingest_decisions.py --role bull --limit 1000
    python scripts/modelforge_ingest_decisions.py --role regime-tagger --limit 5000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("modelforge_ingest_decisions")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]

#: Roles that this script can bootstrap.
BOOTSTRAP_ROLES: tuple[str, ...] = (
    "trading-bull",
    "trading-bear",
    "trading-arbiter",
    "trading-regime-tagger",
)

#: Short-form aliases accepted by --role CLI arg.
_ROLE_ALIASES: dict[str, str] = {
    "bull":          "trading-bull",
    "bear":          "trading-bear",
    "arbiter":       "trading-arbiter",
    "regime-tagger": "trading-regime-tagger",
    "regime_tagger": "trading-regime-tagger",
}

#: Agent names that ingest uses to route to each role.
_ROLE_AGENT: dict[str, str] = {
    "trading-bull":          "risk_debate.aggressive",
    "trading-bear":          "risk_debate.conservative",
    "trading-arbiter":       "risk_debate.neutral",
    "trading-regime-tagger": "regime_tagger",
}

#: Crypto-term blocklist — same as Section A / modelforge_curate.py CRYPTO_TERMS.
#: Applied to bull/bear/arbiter debate text. regime-tagger uses strategy labels,
#: not prose, so it is exempt.
CRYPTO_BLOCKLIST_ROLES: frozenset[str] = frozenset({
    "trading-bull", "trading-bear", "trading-arbiter",
})

CRYPTO_TERMS: tuple[str, ...] = (
    "funding rate", "on-chain", "USDT", "USDC", "BTC", "ETH", "LTC",
    "SOL", "ADA", "perpetual", "leverage", "staking", "mempool",
    "gas fee", "tokenomics", "airdrop", "whale", "24/7", "mining",
    "validator", "halving",
)

#: quanta_schema.decisions.strategy → RegimeLabel enum mapping (Section A).
#: Live quanta-core today emits only two strategy values: mean_rev_bb and trend_follow.
#: The spec's broader set (meta_up_regime, bb_squeeze, etc.) is preserved for
#: forward-compatibility if quanta-core's strategy taxonomy expands.
STRATEGY_TO_REGIME: dict[str, str] = {
    # Live strategies (2026-05-17): mean_rev_bb = mean-reversion bollinger
    # band trade → ranging market; trend_follow = momentum continuation
    # in the prevailing direction → trending_up (we use the bullish label
    # because the strategy fires long-only in quanta-core today; if a
    # short-trend variant is added the mapping must be updated to read
    # the sign from another field).
    "mean_rev_bb":       "ranging",
    "trend_follow":      "trending_up",
    # Forward-compat values (not currently emitted but spec'd in Section A):
    "meta_up_regime":    "trending_up",
    "meta_down_regime":  "trending_down",
    "bb_squeeze":        "ranging",
    "bb_breakout":       "breakout_up",
    "bb_revert":         "ranging",
    # high_vol_* covers any strategy starting with "high_vol"
}

#: Recognised strategies that trigger high_volatility label.
_HIGH_VOL_PREFIX = "high_vol"

#: System messages per role — minimal but informative for training context.
_SYSTEM_MSG: dict[str, str] = {
    "trading-bull": (
        "You are the bullish analyst in a risk debate. "
        "Given the market context, provide a concise bullish thesis "
        "with specific evidence (price levels, indicators, catalysts)."
    ),
    "trading-bear": (
        "You are the bearish analyst in a risk debate. "
        "Given the market context, provide a concise bearish thesis "
        "with specific evidence (price levels, indicators, risks)."
    ),
    "trading-arbiter": (
        "You are the arbiter reviewing a trading debate. "
        "Given the bull and bear arguments, the outcome, and the rationale, "
        "assess the decision quality and recommend a trade direction."
    ),
    "trading-regime-tagger": (
        "You are a market regime classifier. "
        "Given the strategy and market context, output a JSON object "
        "with the regime label: trending_up, trending_down, ranging, "
        "breakout_up, breakout_down, high_volatility, or low_volatility."
    ),
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _raw_root() -> Path:
    override = os.environ.get("DGX_TRAIN_ROOT", "").strip()
    if override:
        return Path(override) / "raw"
    return Path.home() / ".dgx-train" / "raw"


def _has_crypto_term(text: str) -> bool:
    lower = text.lower()
    return any(term.lower() in lower for term in CRYPTO_TERMS)


def _map_strategy_to_regime(strategy: str) -> str | None:
    """Map a quanta_schema.decisions.strategy value to a RegimeLabel string.

    Returns None for unmapped strategies (row is skipped per spec Section A).
    """
    if not strategy:
        return None
    s = str(strategy).strip()
    if s in STRATEGY_TO_REGIME:
        return STRATEGY_TO_REGIME[s]
    if s.startswith(_HIGH_VOL_PREFIX):
        return "high_volatility"
    return None


def _ts_to_iso(ts_val: Any) -> str:
    """Convert a psycopg2/asyncpg timestamp to an ISO string."""
    if ts_val is None:
        return ""
    if isinstance(ts_val, datetime):
        return ts_val.astimezone(timezone.utc).isoformat()
    return str(ts_val)


def _build_row(
    role: str,
    *,
    row_id: int,
    ts_str: str,
    symbol: str,
    user_message: str,
    response_text: str,
    extra_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSONL row in the format modelforge_curate.py expects."""
    agent = _ROLE_AGENT[role]
    return {
        "ts":             ts_str,
        "ticker":         symbol,
        "system_message": _SYSTEM_MSG[role],
        "user_message":   user_message,
        "response":       response_text,
        "pending_outcome": False,
        "outcome_key":    f"{ts_str}|decisions.{row_id}|{agent}",
        "ledger": {
            "agent":    agent,
            "model":    "quanta_schema.decisions",
            "provider": "postgres_bootstrap",
            "tier":     "bootstrap",
            "role":     role,
            "source_id": row_id,
            "valid":    True,
            **(extra_ledger or {}),
        },
    }


def _extract_debate_text(debate: Any, key: str) -> str:
    """Safely extract a string from the JSONB debate column."""
    if isinstance(debate, str):
        try:
            debate = json.loads(debate)
        except (json.JSONDecodeError, TypeError):
            return ""
    if isinstance(debate, dict):
        val = debate.get(key)
        if isinstance(val, str):
            return val.strip()
        if isinstance(val, dict):
            # Sometimes nested as {"text": "..."} — flatten.
            return str(val.get("text") or val.get("response") or "").strip()
    return ""


def _connect_db(db_url: str):
    """Return a psycopg2 connection. Raises on failure."""
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2-binary not installed. Run: pip install psycopg2-binary"
        ) from exc
    return psycopg2.connect(db_url, connect_timeout=10)


# --------------------------------------------------------------------------- #
# Per-role extraction
# --------------------------------------------------------------------------- #

def _extract_bull(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (user_message, response_text) or (None, None) to skip."""
    debate = row.get("debate") or {}
    bull_text = _extract_debate_text(debate, "bull")
    if not bull_text:
        return None, None
    symbol = str(row.get("symbol") or "")
    user_msg = (
        f"Symbol: {symbol}\n"
        f"Strategy: {row.get('strategy') or 'unknown'}\n"
        f"Outcome: {row.get('outcome') or 'unknown'}\n\n"
        f"Provide your bullish thesis for this trade."
    )
    return user_msg, bull_text


def _extract_bear(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (user_message, response_text) or (None, None) to skip."""
    debate = row.get("debate") or {}
    bear_text = _extract_debate_text(debate, "bear")
    if not bear_text:
        return None, None
    symbol = str(row.get("symbol") or "")
    user_msg = (
        f"Symbol: {symbol}\n"
        f"Strategy: {row.get('strategy') or 'unknown'}\n"
        f"Outcome: {row.get('outcome') or 'unknown'}\n\n"
        f"Provide your bearish thesis for this trade."
    )
    return user_msg, bear_text


def _extract_arbiter(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (user_message, response_text) or (None, None) to skip."""
    debate = row.get("debate") or {}
    bull_text = _extract_debate_text(debate, "bull")
    bear_text = _extract_debate_text(debate, "bear")
    rationale = str(row.get("rationale") or "").strip()
    outcome = str(row.get("outcome") or "").strip()

    if not outcome and not rationale:
        return None, None

    symbol = str(row.get("symbol") or "")
    user_msg = (
        f"Symbol: {symbol}\n"
        f"Strategy: {row.get('strategy') or 'unknown'}\n\n"
        f"Bull thesis:\n{bull_text or 'N/A'}\n\n"
        f"Bear thesis:\n{bear_text or 'N/A'}\n\n"
        f"Arbitrate this debate and recommend a trade direction."
    )
    # The arbiter response is the combination of outcome + rationale.
    response_text = f"Outcome: {outcome}\n\nRationale: {rationale}" if rationale else f"Outcome: {outcome}"
    return user_msg, response_text


def _extract_regime_tagger(row: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (user_message, response_text) or (None, None) to skip.

    response_text is a JSON string: {"regime": "<label>"}.
    """
    strategy = str(row.get("strategy") or "").strip()
    regime = _map_strategy_to_regime(strategy)
    if regime is None:
        return None, None  # Skip — no mapping for this strategy.

    symbol = str(row.get("symbol") or "")
    user_msg = (
        f"Symbol: {symbol}\n"
        f"Strategy: {strategy}\n\n"
        f"Classify the current market regime."
    )
    response_text = json.dumps({"regime": regime})
    return user_msg, response_text


_ROLE_EXTRACTORS = {
    "trading-bull":          _extract_bull,
    "trading-bear":          _extract_bear,
    "trading-arbiter":       _extract_arbiter,
    "trading-regime-tagger": _extract_regime_tagger,
}


# --------------------------------------------------------------------------- #
# Main ETL
# --------------------------------------------------------------------------- #

def run_etl(
    roles: list[str],
    *,
    db_url: str,
    limit: int,
    raw_root_path: Path,
) -> dict[str, tuple[int, int]]:
    """Query decisions table and write JSONL files per role.

    Returns dict[role] → (records_written, records_skipped).
    Raises on DB connection error. Never silently drops skips.
    """
    conn = _connect_db(db_url)
    try:
        cur = conn.cursor()
        # Fetch once per ETL run: all rows up to limit, oldest first.
        # We map per-role from the same batch to avoid N queries.
        logger.info("querying quanta_schema.decisions limit=%d ...", limit)
        cur.execute(
            "SELECT id, ts, symbol, strategy, debate, outcome, rationale "
            "FROM quanta_schema.decisions "
            "ORDER BY id ASC LIMIT %s",
            (limit,),
        )
        # Use description to build row dicts — more robust than positional indexing.
        cols = [desc[0] for desc in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        logger.info("fetched %d rows from quanta_schema.decisions", len(rows))
    finally:
        conn.close()

    if not rows:
        logger.warning("No rows returned from quanta_schema.decisions")
        return {role: (0, 0) for role in roles}

    low_id = rows[0]["id"]
    high_id = rows[-1]["id"]

    stats: dict[str, tuple[int, int]] = {}

    for role in roles:
        extractor = _ROLE_EXTRACTORS[role]
        apply_blocklist = role in CRYPTO_BLOCKLIST_ROLES
        out_rows: list[dict[str, Any]] = []
        skipped = 0

        for r in rows:
            user_msg, response_text = extractor(r)
            if user_msg is None or response_text is None:
                skipped += 1
                continue

            # Crypto-term blocklist: only for prose roles (bull/bear/arbiter).
            if apply_blocklist:
                if _has_crypto_term(response_text) or _has_crypto_term(user_msg):
                    skipped += 1
                    continue

            ts_str = _ts_to_iso(r.get("ts"))
            symbol = str(r.get("symbol") or "")
            out_rows.append(_build_row(
                role,
                row_id=int(r["id"]),
                ts_str=ts_str,
                symbol=symbol,
                user_message=user_msg,
                response_text=response_text,
            ))

        written = len(out_rows)
        stats[role] = (written, skipped)

        if not out_rows:
            logger.warning(
                "[%s] 0 records written after blocklist/filter "
                "(total=%d, skipped=%d). "
                "This is expected if all 29k crypto decisions are blocked by the "
                "crypto-term contamination filter (Section A). The track will fail "
                "the N_MIN gate and refuse to train until stock-side records accumulate.",
                role, len(rows), skipped,
            )
            continue

        out_dir = raw_root_path / role
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"decisions_{low_id}_{high_id}.jsonl"
        tmp = out_file.with_suffix(".jsonl.partial")
        with tmp.open("w", encoding="utf-8") as fh:
            for row_dict in out_rows:
                fh.write(json.dumps(row_dict, default=str) + "\n")
        tmp.rename(out_file)
        logger.info("[%s] wrote %d records → %s", role, written, out_file)

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot historical bootstrap from quanta_schema.decisions "
                    "for bull/bear/arbiter/regime-tagger roles.",
    )
    parser.add_argument(
        "--role",
        required=True,
        metavar="ROLE",
        help=(
            "Role(s) to bootstrap. Accepts: bull, bear, arbiter, regime-tagger "
            "(or full trading-* names), or 'all' for all 4 roles."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5000,
        help="Max rows to query from quanta_schema.decisions. Default: 5000 (spec Open Question 3).",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Postgres DSN. Defaults to $TRADEBOT_DATABASE_URL.",
    )
    parser.add_argument(
        "--raw-root",
        default=None,
        help="Override ~/.dgx-train/raw output directory.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stdout summary.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Resolve roles.
    if args.role.lower() == "all":
        roles = list(BOOTSTRAP_ROLES)
    else:
        raw_role = args.role.strip()
        resolved = _ROLE_ALIASES.get(raw_role) or (
            raw_role if raw_role in BOOTSTRAP_ROLES else None
        )
        if resolved is None:
            print(
                f"ERROR: unknown role {raw_role!r}. "
                f"Valid: {sorted(_ROLE_ALIASES)} or 'all'",
                file=sys.stderr,
            )
            return 1
        roles = [resolved]

    # Resolve DB URL.
    db_url = args.db_url or os.environ.get("TRADEBOT_DATABASE_URL", "").strip()
    if not db_url:
        print(
            "ERROR: --db-url not set and TRADEBOT_DATABASE_URL env var is empty. "
            "Pass --db-url or set TRADEBOT_DATABASE_URL.",
            file=sys.stderr,
        )
        return 1

    raw_root_path = Path(args.raw_root) if args.raw_root else _raw_root()

    try:
        stats = run_etl(roles, db_url=db_url, limit=args.limit, raw_root_path=raw_root_path)
    except Exception as exc:
        print(f"ERROR: ETL failed: {exc}", file=sys.stderr)
        logger.exception("ETL crashed")
        return 1

    # Summary output.
    total_written = 0
    any_zero = False
    for role, (written, skipped) in stats.items():
        total_written += written
        if written == 0:
            any_zero = True
        if not args.quiet:
            print(
                f"{role}: written={written} skipped={skipped} "
                f"(total queried={written + skipped})"
            )

    if not args.quiet:
        print(f"\nTotal records written across all roles: {total_written}")
        if any_zero:
            print(
                "\nWARNING: One or more roles produced 0 records. "
                "This is expected for bull/bear/arbiter when ALL crypto decisions "
                "are blocked by the crypto-term contamination filter (Section A). "
                "The N_MIN gate will block training until stock-side records accumulate."
            )

    # Exit nonzero if ALL roles produced 0 records (spec verification step).
    if total_written == 0:
        print(
            "ERROR: 0 records written for ALL roles. "
            "Check DB connectivity, table contents, and crypto-term blocklist.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
