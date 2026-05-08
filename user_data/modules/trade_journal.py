"""
Trade journal — durable per-trade record stored alongside the on-chain DB.

Design choice: reuse the existing SQLite file (`user_data/data/onchain.db`)
so the trading bot has *one* operational data store. The new table is
isolated under the name `trade_journal`; nothing else in onchain_signals
touches it.

Contract
--------

    journal = TradeJournal()
    trade_id = journal.log_entry(
        pair="BTC/USD", direction="long",
        entry_price=65_412.5, stake=1000.0,
        confidence=0.72,
        tft_probs={"down": 0.1, "flat": 0.2, "up": 0.7},
        drl_votes={"ppo": 1, "a2c": 1, "dqn": 0},
        sentiment_score=0.42, sentiment_confidence=0.7,
        regime="trending_up",
        features_used=["%-rsi-period_14", "%-onchain_mvrv", ...],
        reasoning="TFT up=0.7 + meta_signal=+1 + regime=trending_up",
    )
    ...
    journal.log_exit(
        trade_id, exit_price=66_010.0, pnl=59.7, pnl_pct=0.0091,
        exit_reason="freqai_down_regime", duration_min=144,
    )

Export:

    journal.export_csv(start, end, "user_data/logs/journal-2026W19.csv")
    journal.export_markdown(start, end, "user_data/logs/journal-2026W19.md")

Schema
------
The table stores small JSON blobs for the prediction context, which is
sufficient for audit/research without exploding the schema.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


DEFAULT_DB_PATH = Path("user_data/data/onchain.db")
TABLE = "trade_journal"


SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    trade_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id     TEXT,                          -- freqtrade trade.id when known
    pair            TEXT NOT NULL,
    direction       TEXT NOT NULL,                 -- "long" | "short"
    opened_at       TEXT NOT NULL,                 -- ISO-8601 UTC
    closed_at       TEXT,                          -- ISO-8601 UTC; NULL while open
    entry_price     REAL,
    exit_price      REAL,
    stake           REAL,
    pnl             REAL,                          -- in quote currency
    pnl_pct         REAL,                          -- signed return on stake
    duration_min    REAL,
    confidence      REAL,
    tft_probs_json  TEXT,                          -- {{"up":0.7,"flat":0.2,"down":0.1}}
    drl_votes_json  TEXT,                          -- {{"ppo":1,"a2c":1,"dqn":0}}
    sentiment_score REAL,
    sentiment_conf  REAL,
    regime          TEXT,
    exit_reason     TEXT,
    features_json   TEXT,                          -- list of feature column names used
    reasoning       TEXT,                          -- human-readable explanation
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_{TABLE}_opened_at ON {TABLE}(opened_at);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_pair ON {TABLE}(pair);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_external_id ON {TABLE}(external_id);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


@dataclass
class TradeRow:
    trade_id: int
    external_id: str | None
    pair: str
    direction: str
    opened_at: str
    closed_at: str | None
    entry_price: float | None
    exit_price: float | None
    stake: float | None
    pnl: float | None
    pnl_pct: float | None
    duration_min: float | None
    confidence: float | None
    tft_probs: dict
    drl_votes: dict
    sentiment_score: float | None
    sentiment_conf: float | None
    regime: str | None
    exit_reason: str | None
    features_used: list[str]
    reasoning: str | None

    @classmethod
    def from_db_row(cls, r: sqlite3.Row) -> "TradeRow":
        return cls(
            trade_id=r["trade_id"],
            external_id=r["external_id"],
            pair=r["pair"],
            direction=r["direction"],
            opened_at=r["opened_at"],
            closed_at=r["closed_at"],
            entry_price=r["entry_price"],
            exit_price=r["exit_price"],
            stake=r["stake"],
            pnl=r["pnl"],
            pnl_pct=r["pnl_pct"],
            duration_min=r["duration_min"],
            confidence=r["confidence"],
            tft_probs=_safe_json(r["tft_probs_json"]) or {},
            drl_votes=_safe_json(r["drl_votes_json"]) or {},
            sentiment_score=r["sentiment_score"],
            sentiment_conf=r["sentiment_conf"],
            regime=r["regime"],
            exit_reason=r["exit_reason"],
            features_used=_safe_json(r["features_json"]) or [],
            reasoning=r["reasoning"],
        )


def _safe_json(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class TradeJournal:
    """Thread-safe append/update with one connection per call (SQLite is fine for our cadence)."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialised = False
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            # WAL avoids reader/writer locking against the on-chain refresher.
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        if self._initialised:
            return
        with self._init_lock:
            if self._initialised:
                return
            with self._conn() as c:
                c.executescript(SCHEMA_SQL)
            self._initialised = True

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def log_entry(
        self,
        *,
        pair: str,
        direction: str,
        entry_price: float,
        stake: float | None = None,
        confidence: float | None = None,
        tft_probs: Mapping[str, float] | None = None,
        drl_votes: Mapping[str, Any] | None = None,
        sentiment_score: float | None = None,
        sentiment_confidence: float | None = None,
        regime: str | None = None,
        features_used: Sequence[str] | None = None,
        reasoning: str | None = None,
        external_id: str | None = None,
        opened_at: datetime | None = None,
    ) -> int:
        """Insert a new open trade. Returns the journal's trade_id."""
        ts = (opened_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._conn() as c:
            cur = c.execute(
                f"""INSERT INTO {TABLE}
                    (external_id, pair, direction, opened_at, entry_price, stake,
                     confidence, tft_probs_json, drl_votes_json,
                     sentiment_score, sentiment_conf, regime,
                     features_json, reasoning)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    external_id, pair, direction, ts, float(entry_price),
                    None if stake is None else float(stake),
                    None if confidence is None else float(confidence),
                    json.dumps(dict(tft_probs)) if tft_probs is not None else None,
                    json.dumps(dict(drl_votes)) if drl_votes is not None else None,
                    None if sentiment_score is None else float(sentiment_score),
                    None if sentiment_confidence is None else float(sentiment_confidence),
                    regime,
                    json.dumps(list(features_used)) if features_used is not None else None,
                    reasoning,
                ),
            )
            return int(cur.lastrowid)

    def log_exit(
        self,
        trade_id: int,
        *,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        exit_reason: str,
        duration_min: float | None = None,
        closed_at: datetime | None = None,
    ) -> bool:
        ts = (closed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._conn() as c:
            cur = c.execute(
                f"""UPDATE {TABLE}
                       SET exit_price = ?,
                           pnl = ?, pnl_pct = ?,
                           exit_reason = ?,
                           duration_min = ?,
                           closed_at = ?,
                           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                     WHERE trade_id = ?""",
                (
                    float(exit_price), float(pnl), float(pnl_pct),
                    exit_reason,
                    None if duration_min is None else float(duration_min),
                    ts, int(trade_id),
                ),
            )
            return cur.rowcount > 0

    def find_open_by_external_id(self, external_id: str) -> int | None:
        with self._conn() as c:
            row = c.execute(
                f"SELECT trade_id FROM {TABLE} "
                f"WHERE external_id = ? AND closed_at IS NULL "
                f"ORDER BY trade_id DESC LIMIT 1",
                (external_id,),
            ).fetchone()
            return int(row["trade_id"]) if row else None

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_trade(self, trade_id: int) -> TradeRow | None:
        with self._conn() as c:
            r = c.execute(
                f"SELECT * FROM {TABLE} WHERE trade_id = ?", (int(trade_id),),
            ).fetchone()
            return TradeRow.from_db_row(r) if r else None

    def query(
        self, start: datetime | None = None, end: datetime | None = None,
        pair: str | None = None, only_closed: bool = False,
    ) -> list[TradeRow]:
        clauses, params = [], []
        if start is not None:
            clauses.append("opened_at >= ?")
            params.append(start.astimezone(timezone.utc).isoformat())
        if end is not None:
            clauses.append("opened_at < ?")
            params.append(end.astimezone(timezone.utc).isoformat())
        if pair is not None:
            clauses.append("pair = ?")
            params.append(pair)
        if only_closed:
            clauses.append("closed_at IS NOT NULL")
        sql = f"SELECT * FROM {TABLE}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY opened_at ASC"
        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
            return [TradeRow.from_db_row(r) for r in rows]

    def stats(self, start: datetime | None = None, end: datetime | None = None) -> dict:
        rows = self.query(start=start, end=end, only_closed=True)
        if not rows:
            return {"trades": 0}
        pnls = [float(r.pnl or 0.0) for r in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        return {
            "trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(rows),
            "total_pnl": sum(pnls),
            "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "profit_factor": (
                sum(wins) / abs(sum(losses)) if losses else float("inf")
            ),
        }

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    CSV_HEADER = (
        "trade_id", "external_id", "pair", "direction",
        "opened_at", "closed_at",
        "entry_price", "exit_price", "stake",
        "pnl", "pnl_pct", "duration_min",
        "confidence", "tft_probs", "drl_votes",
        "sentiment_score", "sentiment_conf", "regime",
        "exit_reason", "features_used", "reasoning",
    )

    def export_csv(
        self, start: datetime | None, end: datetime | None, path: str | Path,
    ) -> int:
        rows = self.query(start=start, end=end)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(self.CSV_HEADER)
            for r in rows:
                w.writerow([
                    r.trade_id, r.external_id or "", r.pair, r.direction,
                    r.opened_at, r.closed_at or "",
                    r.entry_price, r.exit_price, r.stake,
                    r.pnl, r.pnl_pct, r.duration_min,
                    r.confidence,
                    json.dumps(r.tft_probs or {}),
                    json.dumps(r.drl_votes or {}),
                    r.sentiment_score, r.sentiment_conf, r.regime or "",
                    r.exit_reason or "",
                    json.dumps(r.features_used or []),
                    r.reasoning or "",
                ])
        logger.info("[journal] CSV: %d rows → %s", len(rows), path)
        return len(rows)

    def export_markdown(
        self, start: datetime | None, end: datetime | None, path: str | Path,
    ) -> int:
        rows = self.query(start=start, end=end, only_closed=False)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        s = self.stats(start=start, end=end)
        lines = [
            "# Trade journal",
            "",
            f"- **Window**: "
            f"{start.isoformat() if start else '—'} → "
            f"{end.isoformat() if end else '—'}",
            f"- **Trades**: {s.get('trades', 0)}"
            + (f"  ({s.get('wins',0)}W / {s.get('losses',0)}L; "
               f"win-rate {s.get('win_rate',0):.1%})" if s.get('trades') else ""),
            (f"- **Total P&L**: ${s['total_pnl']:,.2f}"
             if "total_pnl" in s else ""),
            (f"- **Avg win / avg loss**: ${s['avg_win']:,.2f} / "
             f"${s['avg_loss']:,.2f}" if "avg_win" in s else ""),
            (f"- **Profit factor**: {s['profit_factor']:.2f}"
             if "profit_factor" in s else ""),
            "",
            "| # | Pair | Dir | Open (UTC) | Close (UTC) | Entry | Exit | P&L | % | Reason | Conf | Regime |",
            "|---|------|-----|------------|-------------|-------|------|-----|---|--------|------|--------|",
        ]
        for r in rows:
            lines.append(
                f"| {r.trade_id} | `{r.pair}` | {r.direction} | "
                f"{(r.opened_at or '')[:19]} | {(r.closed_at or '')[:19]} | "
                f"{r.entry_price or 0:.4f} | {r.exit_price or 0:.4f} | "
                f"{r.pnl or 0:+,.2f} | {((r.pnl_pct or 0) * 100):+.2f}% | "
                f"`{r.exit_reason or ''}` | "
                f"{((r.confidence or 0) * 100):.0f}% | "
                f"{r.regime or ''} |"
            )
        path.write_text("\n".join(line for line in lines if line is not None))
        logger.info("[journal] Markdown: %d rows → %s", len(rows), path)
        return len(rows)
