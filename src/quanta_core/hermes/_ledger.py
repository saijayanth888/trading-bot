"""Read-only ledger query helpers for Hermes modules.

Hermes is a *consumer* of the ledger — it never writes trades or positions.
This module wraps the small set of read queries the seven modules share so
each module's body stays focused on its own concerns.

Per doc §11 §3.1 the dependency rule is::

    reflector → ledger.postgres, models.registry, agents.reflector
    weekly_publisher → ledger.postgres, observability.metrics
    briefer → data.calendar, data.universe, models.registry, ledger.postgres
    post_mortem → ledger.postgres
    healthcheck → exchanges.alpaca, exchanges.coinbase, ledger.postgres,
                  models.registry

Implementation note: the legacy schema lives across several tables today
(``trade_journal``, ``trades``, ``reflector_lessons`` …) and the V4 design
is converging on a single ``ledger.trades`` table.  This module deliberately
exposes a small typed surface — :class:`TradeRow` — so callers do not depend
on column-level details.  When the canonical schema lands the column-name
mappings below are the *only* place the change touches.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

try:  # psycopg is a hard dep, but mirror nightly_reflector.py's defensive shape
    import psycopg
    from psycopg.rows import dict_row
    _HAVE_PG = True
except Exception:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    _HAVE_PG = False


@dataclass
class TradeRow:
    """Slim, public-facing view of a closed trade.

    Only fields downstream modules render are surfaced.  Anything else stays
    in the raw row and modules can ask for it explicitly when needed.
    """

    trade_id: str
    pair: str
    side: str
    entry_price: float | None
    exit_price: float | None
    entry_ts: datetime | None
    exit_ts: datetime | None
    pnl: float | None
    pnl_pct: float | None
    strategy: str | None
    regime: str | None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class LedgerClient:
    """Thin connection wrapper.

    ``dsn`` is taken from :class:`HermesConfig.postgres_dsn`.  When
    ``dsn is None`` every method returns an empty iterable + logs a
    ``ledger_unavailable`` warning — Hermes fails-open on infra per doc §7.
    """

    dsn: str | None
    timeout_seconds: float = 8.0
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("quanta_core.hermes.ledger")
    )

    # ----- public API ---------------------------------------------------

    @property
    def available(self) -> bool:
        return _HAVE_PG and bool(self.dsn)

    def closed_trades_for_day(
        self, trading_day: date
    ) -> Sequence[TradeRow]:
        """Return trades closed on ``trading_day`` (UTC date).

        Falls back to an empty list on any infra error.
        """

        sql = (
            "SELECT id::text AS trade_id, pair, side, "
            "entry_price, exit_price, "
            "entry_ts AT TIME ZONE 'UTC' AS entry_ts, "
            "exit_ts AT TIME ZONE 'UTC' AS exit_ts, "
            "pnl, pnl_pct, strategy, regime "
            "FROM ledger.trades "
            "WHERE exit_ts::date = %s AND is_open = false "
            "ORDER BY exit_ts ASC"
        )
        return list(self._fetch(sql, (trading_day,)))

    def closed_trades_for_range(
        self, start: date, end: date
    ) -> Sequence[TradeRow]:
        """Return trades closed in ``[start, end]`` (inclusive)."""

        sql = (
            "SELECT id::text AS trade_id, pair, side, "
            "entry_price, exit_price, "
            "entry_ts AT TIME ZONE 'UTC' AS entry_ts, "
            "exit_ts AT TIME ZONE 'UTC' AS exit_ts, "
            "pnl, pnl_pct, strategy, regime "
            "FROM ledger.trades "
            "WHERE exit_ts::date BETWEEN %s AND %s AND is_open = false "
            "ORDER BY exit_ts ASC"
        )
        return list(self._fetch(sql, (start, end)))

    def open_positions(self) -> Sequence[TradeRow]:
        """Return rows for currently-open positions."""

        sql = (
            "SELECT id::text AS trade_id, pair, side, "
            "entry_price, NULL::numeric AS exit_price, "
            "entry_ts AT TIME ZONE 'UTC' AS entry_ts, "
            "NULL::timestamp AS exit_ts, "
            "NULL::numeric AS pnl, NULL::numeric AS pnl_pct, "
            "strategy, regime "
            "FROM ledger.trades "
            "WHERE is_open = true "
            "ORDER BY entry_ts ASC"
        )
        return list(self._fetch(sql, ()))

    def ping(self) -> bool:
        """Return ``True`` if a trivial ``SELECT 1`` succeeds."""

        if not self.available:
            return False
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as exc:  # pragma: no cover — depends on infra
            self.logger.warning("postgres ping failed: %s", exc)
            return False

    # ----- internals ----------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        if not self.available or self.dsn is None:  # pragma: no cover
            raise RuntimeError("ledger unavailable")
        assert psycopg is not None  # mypy
        with psycopg.connect(
            self.dsn,
            connect_timeout=int(self.timeout_seconds),
            row_factory=dict_row,
        ) as conn:
            yield conn

    def _fetch(
        self, sql: str, params: Sequence[Any]
    ) -> Iterable[TradeRow]:
        if not self.available:
            self.logger.warning(
                "ledger_unavailable: skipping query (no DSN or psycopg missing)"
            )
            return iter(())
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except Exception as exc:  # pragma: no cover — depends on infra
            self.logger.warning("ledger query failed: %s", exc)
            return iter(())
        return [self._row_to_trade(row) for row in rows]

    @staticmethod
    def _row_to_trade(row: Mapping[str, Any]) -> TradeRow:
        return TradeRow(
            trade_id=str(row.get("trade_id", "")),
            pair=str(row.get("pair", "")),
            side=str(row.get("side", "")),
            entry_price=_as_float(row.get("entry_price")),
            exit_price=_as_float(row.get("exit_price")),
            entry_ts=_as_dt(row.get("entry_ts")),
            exit_ts=_as_dt(row.get("exit_ts")),
            pnl=_as_float(row.get("pnl")),
            pnl_pct=_as_float(row.get("pnl_pct")),
            strategy=_as_str_or_none(row.get("strategy")),
            regime=_as_str_or_none(row.get("regime")),
            raw=dict(row),
        )


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return None


def _as_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = ["LedgerClient", "TradeRow"]
