"""
wheel.state — local journal for the wheel strategy.

Stores positions, trade log, and cumulative P&L in JSON files under
stocks/wheel/state/. Atomic writes via shark.memory.atomic so a crash
mid-write doesn't corrupt the journal.

Files:
    state/positions.json    open CSP / CC / shares with entry context
    state/trades.jsonl      append-only ledger of every fill (closed cycles)
    state/kill_flags.json   per-ticker kill flags (90-day cooldown after big loss)

state/ as a whole is gitignored (regenerable runtime data); trades.jsonl is
optionally archived later for audit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

# Reuse shark's atomic write (already battle-tested with file locking)
from shark.memory.atomic import atomic_write_text

logger = logging.getLogger(__name__)

_STATE_DIR = Path(__file__).resolve().parent / "state"
_POSITIONS_FILE = _STATE_DIR / "positions.json"
_TRADES_FILE = _STATE_DIR / "trades.jsonl"
_KILL_FLAGS_FILE = _STATE_DIR / "kill_flags.json"


def _ensure_dir() -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Position:
    """An open wheel position (short put, short call, or held shares)."""
    underlying: str
    contract_symbol: str  # "" for shares, option symbol otherwise
    kind: str  # "short_put" | "short_call" | "long_shares"
    qty: int  # positive number; convention: 1 contract = 100 shares
    strike: float = 0.0  # 0 for shares
    expiry: str | None = None  # ISO date for options, None for shares
    entry_credit: float = 0.0  # premium collected (puts/calls)
    entry_price: float = 0.0  # cost basis (shares) — also strike when assigned
    opened_at: str = ""  # ISO8601
    source: str = ""  # Free-form provenance tag, e.g. "wheel_assignment".
    status: str = ""  # "" (open), "assigned", "called_away" — informational only.


@dataclass
class TradeRecord:
    """An append-only ledger entry. One per closed cycle."""
    timestamp: str  # ISO8601
    underlying: str
    cycle: str  # "csp_close" | "csp_assigned" | "cc_close" | "cc_called_away"
    pnl: float  # realized USD on this leg
    notes: str = ""


# ── Positions ───────────────────────────────────────────────────────────────


def load_positions() -> list[Position]:
    if not _POSITIONS_FILE.exists():
        return []
    try:
        raw = json.loads(_POSITIONS_FILE.read_text())
        return [Position(**r) for r in raw]
    except (json.JSONDecodeError, TypeError) as exc:
        logger.error("positions.json corrupt: %s — starting fresh", exc)
        return []


def save_positions(positions: list[Position]) -> None:
    _ensure_dir()
    payload = json.dumps([asdict(p) for p in positions], indent=2)
    atomic_write_text(_POSITIONS_FILE, payload)


def add_position(p: Position) -> None:
    positions = load_positions()
    positions.append(p)
    save_positions(positions)


def remove_position(contract_symbol: str) -> None:
    positions = [p for p in load_positions() if p.contract_symbol != contract_symbol]
    save_positions(positions)


def update_position(contract_symbol: str, **changes) -> bool:
    """Update fields on an existing position. Returns True if a row was changed.

    Used by wheel.runner.assignment_check() to mark a short_put as
    `status="assigned"` rather than deleting it outright — so the trade
    log retains the linkage between the short put and the long shares
    that were created from it.
    """
    positions = load_positions()
    changed = False
    for p in positions:
        if p.contract_symbol == contract_symbol:
            for k, v in changes.items():
                if hasattr(p, k):
                    setattr(p, k, v)
                    changed = True
    if changed:
        save_positions(positions)
    return changed


def find_open_csp(underlying: str) -> Position | None:
    """Find any currently-open short put on this underlying."""
    for p in load_positions():
        if p.kind == "short_put" and p.underlying == underlying:
            return p
    return None


def find_open_cc(underlying: str) -> Position | None:
    for p in load_positions():
        if p.kind == "short_call" and p.underlying == underlying:
            return p
    return None


def shares_held(underlying: str) -> int:
    return sum(
        p.qty for p in load_positions()
        if p.kind == "long_shares" and p.underlying == underlying
    )


# ── Trade log ───────────────────────────────────────────────────────────────


def append_trade(rec: TradeRecord) -> None:
    _ensure_dir()
    line = json.dumps(asdict(rec))
    with _TRADES_FILE.open("a") as f:
        f.write(line + "\n")


def cumulative_pnl(since: date | None = None) -> float:
    if not _TRADES_FILE.exists():
        return 0.0
    total = 0.0
    cutoff = since.isoformat() if since else None
    with _TRADES_FILE.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and rec.get("timestamp", "") < cutoff:
                continue
            total += float(rec.get("pnl", 0.0))
    return total


def cumulative_pnl_for(underlying: str, since: date | None = None) -> float:
    """Per-ticker cumulative realized P&L since `since` (date).

    Used by the wheel runner's kill_loss_per_cycle gate (P1-S5) to walk
    away from a ticker that's bled too much in the current pilot window.
    """
    if not _TRADES_FILE.exists():
        return 0.0
    total = 0.0
    cutoff = since.isoformat() if since else None
    with _TRADES_FILE.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("underlying") != underlying:
                continue
            if cutoff and rec.get("timestamp", "") < cutoff:
                continue
            total += float(rec.get("pnl", 0.0))
    return total


# ── Per-ticker kill flags ──────────────────────────────────────────────────


def _load_kill_flags() -> dict[str, str]:
    """Returns {underlying: ISO date when flag expires}."""
    if not _KILL_FLAGS_FILE.exists():
        return {}
    try:
        return json.loads(_KILL_FLAGS_FILE.read_text())
    except json.JSONDecodeError:
        return {}


def _save_kill_flags(flags: dict[str, str]) -> None:
    _ensure_dir()
    atomic_write_text(_KILL_FLAGS_FILE, json.dumps(flags, indent=2))


def is_killed(underlying: str) -> bool:
    flags = _load_kill_flags()
    expiry = flags.get(underlying)
    if not expiry:
        return False
    try:
        return date.fromisoformat(expiry) >= date.today()
    except ValueError:
        return False


def kill_ticker(underlying: str, days: int = 90) -> None:
    flags = _load_kill_flags()
    flags[underlying] = (date.today() + timedelta(days=days)).isoformat()
    _save_kill_flags(flags)
    logger.warning("Wheel kill flag set on %s for %d days", underlying, days)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
