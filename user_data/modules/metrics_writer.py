"""
InfluxDB writer for Grafana dashboards.

DEPRECATED 2026-05-13 — Grafana + InfluxDB containers were removed
from docker-compose.yml on 2026-05-12 (commit b43b1b7). This module
remains importable so legacy callers don't crash, but writes are
no-ops by default (INFLUX_ENABLED defaults to "0"). To re-enable,
set INFLUX_ENABLED=1 AND ship the influxdb container back. The
replacement substrate is `src/quanta_core/observability/v4_buffer`
plus the dashboard's /api/ops/* probes. See
docs/V4_SHADOW_MODE_DESIGN.md for the cutover blueprint.

The bot's hot path doesn't block on metric writes — every write goes
into a bounded background queue and is flushed by a worker thread that
batches up to `batch_size` points before POSTing. If the InfluxDB
container is down the writer logs a single warning per minute and keeps
queueing (or drops once `max_queue` is reached).

Env-driven configuration:

    INFLUX_URL=http://influxdb:8086
    INFLUX_TOKEN=<long-lived API token>
    INFLUX_ORG=trading-bot
    INFLUX_BUCKET=trading

Six measurements (mapped to Grafana panels):

    pnl                tags: pair         fields: equity, peak_equity, drawdown, daily_pnl, cumulative_pnl
    trades             tags: pair, side   fields: pnl, pnl_pct, confidence, duration_min, win
    sharpe             tags: window       fields: sharpe
    win_rate           tags: window       fields: win_rate, n
    regime             tags: pair, label  fields: probability, confidence, count=1
    sentiment          tags: pair         fields: score, confidence, price
    evolution          tags: member_id    fields: fitness, sharpe, max_drawdown, generation, is_champion

Hourly snapshot helper: `write_hourly_snapshot()` consolidates the
"slow" metrics (Sharpe, win rate, equity) so the user doesn't have to
schedule six separate cron entries.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)

try:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import SYNCHRONOUS
    _INFLUX_AVAILABLE = True
except Exception:
    InfluxDBClient = None
    Point = None
    WritePrecision = None
    SYNCHRONOUS = None
    _INFLUX_AVAILABLE = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class InfluxConfig:
    url: str = "http://influxdb:8086"
    token: str = ""
    org: str = "trading-bot"
    bucket: str = "trading"
    batch_size: int = 50
    flush_interval_sec: float = 5.0
    max_queue: int = 10_000
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "InfluxConfig":
        return cls(
            url=os.environ.get("INFLUX_URL", "http://influxdb:8086"),
            token=os.environ.get("INFLUX_TOKEN", ""),
            org=os.environ.get("INFLUX_ORG", "trading-bot"),
            bucket=os.environ.get("INFLUX_BUCKET", "trading"),
            enabled=os.environ.get("INFLUX_ENABLED", "0") == "1",
        )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class MetricsWriter:
    """Background-batched InfluxDB writer."""

    def __init__(
        self, config: InfluxConfig | None = None, *, client: Any = None,
    ) -> None:
        self.cfg = config or InfluxConfig.from_env()
        self._client_inst = client
        self._queue: queue.Queue = queue.Queue(maxsize=self.cfg.max_queue)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_warn = 0.0

        if not _INFLUX_AVAILABLE and client is None:
            logger.warning(
                "[metrics] influxdb-client not installed; writes will be no-ops"
            )
            self.cfg.enabled = False
        elif not self.cfg.token and client is None and self.cfg.enabled:
            logger.warning(
                "[metrics] INFLUX_TOKEN unset; writes disabled until a token is provided"
            )
            self.cfg.enabled = False

        if self.cfg.enabled:
            self._start_worker()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    # ------------------------------------------------------------------
    # Public write helpers
    # ------------------------------------------------------------------

    def write_pnl(
        self, *, pair: str | None = None,
        equity: float | None = None,
        peak_equity: float | None = None,
        drawdown: float | None = None,
        daily_pnl: float | None = None,
        cumulative_pnl: float | None = None,
        ts: datetime | None = None,
    ) -> None:
        fields: dict[str, float] = {}
        for k, v in (
            ("equity", equity), ("peak_equity", peak_equity),
            ("drawdown", drawdown), ("daily_pnl", daily_pnl),
            ("cumulative_pnl", cumulative_pnl),
        ):
            if v is not None:
                fields[k] = float(v)
        if not fields:
            return
        self._enqueue("pnl", tags={"pair": pair or "portfolio"}, fields=fields, ts=ts)

    def write_trade(
        self, *, pair: str, side: str,
        pnl: float, pnl_pct: float,
        confidence: float | None = None,
        duration_min: float | None = None,
        ts: datetime | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "pnl": float(pnl),
            "pnl_pct": float(pnl_pct),
            "win": int(pnl > 0),
        }
        if confidence is not None:
            fields["confidence"] = float(confidence)
        if duration_min is not None:
            fields["duration_min"] = float(duration_min)
        self._enqueue("trades", tags={"pair": pair, "side": side}, fields=fields, ts=ts)

    def write_sharpe(self, sharpe: float, window: str = "30d", ts: datetime | None = None) -> None:
        self._enqueue("sharpe", tags={"window": window}, fields={"sharpe": float(sharpe)}, ts=ts)

    def write_win_rate(self, win_rate: float, n: int, window: str = "30d", ts: datetime | None = None) -> None:
        self._enqueue(
            "win_rate", tags={"window": window},
            fields={"win_rate": float(win_rate), "n": int(n)}, ts=ts,
        )

    def write_regime(
        self, *, pair: str, label: str,
        probability: float | None = None,
        confidence: float | None = None,
        ts: datetime | None = None,
    ) -> None:
        fields: dict[str, Any] = {"count": 1}
        if probability is not None:
            fields["probability"] = float(probability)
        if confidence is not None:
            fields["confidence"] = float(confidence)
        self._enqueue("regime", tags={"pair": pair, "label": label}, fields=fields, ts=ts)

    def write_sentiment(
        self, *, pair: str, score: float, confidence: float, price: float | None = None,
        ts: datetime | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "score": float(score),
            "confidence": float(confidence),
        }
        if price is not None:
            fields["price"] = float(price)
        self._enqueue("sentiment", tags={"pair": pair}, fields=fields, ts=ts)

    def write_evolution(
        self, *, member_id: str, fitness: float, generation: int,
        sharpe: float | None = None, max_drawdown: float | None = None,
        is_champion: bool = False,
        ts: datetime | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "fitness": float(fitness),
            "generation": int(generation),
            "is_champion": int(bool(is_champion)),
        }
        if sharpe is not None:
            fields["sharpe"] = float(sharpe)
        if max_drawdown is not None:
            fields["max_drawdown"] = float(max_drawdown)
        self._enqueue("evolution", tags={"member_id": member_id}, fields=fields, ts=ts)

    def write_hourly_snapshot(
        self, *,
        equity: float, peak_equity: float, drawdown: float,
        daily_pnl: float, cumulative_pnl: float,
        sharpe_30d: float | None = None,
        win_rate_30d: float | None = None, win_rate_n: int = 0,
        regime: tuple[str, str] | None = None,
        ts: datetime | None = None,
    ) -> None:
        """One-shot consolidation of the slow Grafana panels."""
        ts = ts or datetime.now(timezone.utc)
        self.write_pnl(
            equity=equity, peak_equity=peak_equity,
            drawdown=drawdown, daily_pnl=daily_pnl,
            cumulative_pnl=cumulative_pnl, ts=ts,
        )
        if sharpe_30d is not None:
            self.write_sharpe(sharpe_30d, window="30d", ts=ts)
        if win_rate_30d is not None:
            self.write_win_rate(win_rate_30d, win_rate_n, window="30d", ts=ts)
        if regime is not None:
            pair, label = regime
            self.write_regime(pair=pair, label=label, ts=ts)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=timeout)
        if self._client_inst is not None and hasattr(self._client_inst, "close"):
            try:
                self._client_inst.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _enqueue(
        self, measurement: str,
        *, tags: Mapping[str, str], fields: Mapping[str, Any],
        ts: datetime | None = None,
    ) -> None:
        if not self.enabled:
            return
        item = {
            "measurement": measurement,
            "tags": {k: str(v) for k, v in tags.items() if v is not None},
            "fields": dict(fields),
            "ts": ts or datetime.now(timezone.utc),
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            now = time.time()
            if now - self._last_warn > 60:
                logger.warning(
                    "[metrics] queue full (max=%d); dropping points until drained",
                    self.cfg.max_queue,
                )
                self._last_warn = now

    def _start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._run, name="metrics_writer", daemon=True,
        )
        self._worker.start()

    def _run(self) -> None:
        write_api = self._get_write_api()
        if write_api is None:
            return
        batch: list[dict] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
                batch.append(item)
            except queue.Empty:
                pass
            now = time.time()
            should_flush = (
                len(batch) >= self.cfg.batch_size
                or (batch and now - last_flush >= self.cfg.flush_interval_sec)
            )
            if should_flush:
                self._flush(write_api, batch)
                batch.clear()
                last_flush = now
        # drain on shutdown
        if batch:
            self._flush(write_api, batch)

    def _get_write_api(self):
        if self._client_inst is not None:
            return self._client_inst
        if not _INFLUX_AVAILABLE:
            return None
        try:
            client = InfluxDBClient(
                url=self.cfg.url, token=self.cfg.token, org=self.cfg.org,
                timeout=10_000,
            )
            self._client_inst = client
            return client.write_api(write_options=SYNCHRONOUS)
        except Exception as exc:
            logger.warning("[metrics] failed to construct InfluxDB client: %s", exc)
            return None

    def _flush(self, write_api, batch: list[dict]) -> None:
        if not batch:
            return
        if Point is None:
            return
        try:
            points = [self._to_point(b) for b in batch]
            write_api.write(bucket=self.cfg.bucket, org=self.cfg.org, record=points)
        except Exception as exc:
            now = time.time()
            if now - self._last_warn > 60:
                logger.warning("[metrics] flush failed: %s", exc)
                self._last_warn = now

    @staticmethod
    def _to_point(item: dict):
        p = Point(item["measurement"]).time(
            item["ts"], write_precision=WritePrecision.NS,
        )
        for k, v in item["tags"].items():
            p = p.tag(k, str(v))
        for k, v in item["fields"].items():
            if isinstance(v, bool):
                v = int(v)
            if isinstance(v, (int, float, str)):
                p = p.field(k, v)
        return p
