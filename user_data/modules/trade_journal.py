"""
Trade journal — durable per-trade record stored in PostgreSQL.

Schema lives in `user_data/data/schema.sql` and is created once by
`db.ensure_schema()`. New rows are inserted on entry; the same row is
updated on exit with closing-price + P&L + duration. JSON-typed columns
(`tft_probs`, `drl_votes`, `features_used`) use the JSONB native type.

Contract is unchanged from the SQLite version:

    j = TradeJournal()
    trade_id = j.log_entry(pair="BTC/USD", direction="long", entry_price=...,
                           tft_probs={...}, drl_votes={...}, ...)
    j.log_exit(trade_id, exit_price=..., pnl=..., pnl_pct=..., exit_reason=...)
    j.export_csv(start, end, "out.csv")
    j.export_markdown(start, end, "out.md")
    s = j.stats(start, end)
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import db

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _coerce_dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    s = str(v).replace("Z", "+00:00") if str(v).endswith("Z") else str(v)
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except Exception:
        return None


@dataclass
class TradeRow:
    trade_id: int
    external_id: str | None
    pair: str
    direction: str
    opened_at: datetime
    closed_at: datetime | None
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
    def from_db_row(cls, r: Mapping[str, Any]) -> TradeRow:
        return cls(
            trade_id=int(r["trade_id"]),
            external_id=r.get("external_id"),
            pair=str(r["pair"]),
            direction=str(r["direction"]),
            opened_at=_coerce_dt(r["opened_at"]),
            closed_at=_coerce_dt(r.get("closed_at")),
            entry_price=r.get("entry_price"),
            exit_price=r.get("exit_price"),
            stake=r.get("stake"),
            pnl=r.get("pnl"),
            pnl_pct=r.get("pnl_pct"),
            duration_min=r.get("duration_min"),
            confidence=r.get("confidence"),
            tft_probs=_safe_json(r.get("tft_probs")) or {},
            drl_votes=_safe_json(r.get("drl_votes")) or {},
            sentiment_score=r.get("sentiment_score"),
            sentiment_conf=r.get("sentiment_conf"),
            regime=r.get("regime"),
            exit_reason=r.get("exit_reason"),
            features_used=_safe_json(r.get("features_used")) or [],
            reasoning=r.get("reasoning"),
        )


def _safe_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class TradeJournal:
    """Postgres-backed trade ledger. Same constructor signature as before."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        # `db_path` is accepted for backward compatibility with the
        # SQLite version; ignored — Postgres connection comes from the
        # shared pool. Tests set DATABASE_URL to a per-test schema/db.
        self._db_path = db_path

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
        ts = (opened_at or _utc_now()).astimezone(UTC)
        row = db.execute_returning(
            """
            INSERT INTO trade_journal
                (external_id, pair, direction, opened_at, entry_price, stake,
                 confidence, tft_probs, drl_votes,
                 sentiment_score, sentiment_conf, regime,
                 features_used, reasoning)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb, %s)
            RETURNING trade_id
            """,
            (
                external_id, pair, direction, ts,
                float(entry_price),
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
        return int(row["trade_id"])

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
        ts = (closed_at or _utc_now()).astimezone(UTC)
        affected = db.execute_one(
            """
            UPDATE trade_journal
               SET exit_price = %s,
                   pnl = %s, pnl_pct = %s,
                   exit_reason = %s,
                   duration_min = %s,
                   closed_at = %s,
                   updated_at = NOW()
             WHERE trade_id = %s
            """,
            (
                float(exit_price), float(pnl), float(pnl_pct),
                exit_reason,
                None if duration_min is None else float(duration_min),
                ts, int(trade_id),
            ),
        )
        return affected > 0

    def find_open_by_external_id(self, external_id: str) -> int | None:
        row = db.fetch_one(
            "SELECT trade_id FROM trade_journal "
            "WHERE external_id = %s AND closed_at IS NULL "
            "ORDER BY trade_id DESC LIMIT 1",
            (external_id,),
        )
        return int(row["trade_id"]) if row else None

    def find_open_by_pair_and_price(
        self, pair: str, entry_price: float, tolerance_pct: float = 0.001,
    ) -> int | None:
        """Restart-safe correlation key for close-side updates.

        The in-memory `_journal_id_by_trade` mapping the strategy uses to
        match freqtrade.trade.id → trade_journal.trade_id is wiped on every
        freqtrade restart. This DB-direct lookup matches by `pair` plus
        entry-price proximity (0.1% default tolerance to absorb decimal
        rounding) on the latest still-open journal row — survives restarts
        and doesn't depend on the strategy passing an external_id.
        """
        row = db.fetch_one(
            "SELECT trade_id FROM trade_journal "
            " WHERE pair = %s "
            "   AND closed_at IS NULL "
            "   AND ABS(entry_price - %s) / NULLIF(entry_price, 0) < %s "
            " ORDER BY opened_at DESC, trade_id DESC LIMIT 1",
            (pair, float(entry_price), float(tolerance_pct)),
        )
        return int(row["trade_id"]) if row else None

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_trade(self, trade_id: int) -> TradeRow | None:
        row = db.fetch_one(
            "SELECT * FROM trade_journal WHERE trade_id = %s",
            (int(trade_id),),
        )
        return TradeRow.from_db_row(row) if row else None

    def query(
        self, start: datetime | None = None, end: datetime | None = None,
        pair: str | None = None, only_closed: bool = False,
    ) -> list[TradeRow]:
        clauses, params = [], []
        if start is not None:
            clauses.append("opened_at >= %s")
            params.append(start.astimezone(UTC))
        if end is not None:
            clauses.append("opened_at < %s")
            params.append(end.astimezone(UTC))
        if pair is not None:
            clauses.append("pair = %s")
            params.append(pair)
        if only_closed:
            clauses.append("closed_at IS NOT NULL")
        sql = "SELECT * FROM trade_journal"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY opened_at ASC"
        rows = db.fetch_all(sql, tuple(params))
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
                    r.opened_at.isoformat() if r.opened_at else "",
                    r.closed_at.isoformat() if r.closed_at else "",
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
            opened = r.opened_at.isoformat() if r.opened_at else ""
            closed = r.closed_at.isoformat() if r.closed_at else ""
            lines.append(
                f"| {r.trade_id} | `{r.pair}` | {r.direction} | "
                f"{opened[:19]} | {closed[:19]} | "
                f"{r.entry_price or 0:.4f} | {r.exit_price or 0:.4f} | "
                f"{r.pnl or 0:+,.2f} | {((r.pnl_pct or 0) * 100):+.2f}% | "
                f"`{r.exit_reason or ''}` | "
                f"{((r.confidence or 0) * 100):.0f}% | "
                f"{r.regime or ''} |"
            )
        path.write_text("\n".join(line for line in lines if line is not None))
        logger.info("[journal] Markdown: %d rows → %s", len(rows), path)
        return len(rows)
