"""
Coinbase Advanced Trade execution engine.

Limit-orders-only wrapper around the official `coinbase-advanced-py`
SDK with the safety features Freqtrade's CCXT path doesn't provide
out of the box:

    - Pre-flight slippage check: refuses to place if best ask/bid has
      drifted > slippage_pct from the strategy's signal price.
    - Exponential-backoff retry on transient errors (3 attempts).
    - Order timeout: cancels any unfilled order after `timeout_sec` (60s).
    - Partial-fill tracking: returns cumulative filled size + remainder.
    - Structured order log to `user_data/logs/execution.log`.

Two ways to use it:

    (a) Standalone — bypass Freqtrade's order pipeline entirely. Useful
        for OOB / bracket / adjustment orders the bot needs to place
        outside the strategy main loop.

    (b) Reference — read the limits enforced here and replicate them
        in Freqtrade's `order_types` + `unfilledtimeout` config so the
        native CCXT path makes the same guarantees. The bundled
        `config.json` does this; see `order_types` and `unfilledtimeout`.

Auth: pass `api_key` and `api_secret` from environment. The SDK uses
the new ECDSA / HS-style keys (no passphrase). Dry-run mode (the
default for `dry_run=True`) makes no network calls and returns
deterministic synthetic orders so test suites can run unchanged.

Usage:

    eng = ExecutionEngine.from_config_file("user_data/config.json")
    rep = eng.place_limit("BTC-USD", side="BUY", base_size="0.001",
                          limit_price="65000.00", signal_price=64950.00)
    if rep.status == "FILLED": ...

Order log lines look like:

    2026-05-08T12:34:56Z PLACE   id=abcd...  BUY 0.001 BTC-USD @ 65000.00
    2026-05-08T12:35:01Z FILL    id=abcd...  filled=0.001 (100.0%)
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dedicated rotating execution log — separate from the main freqtrade log so
# every order lifecycle event is auditable on its own.
# ---------------------------------------------------------------------------


def _setup_execution_logger(path: Path | str) -> logging.Logger:
    log = logging.getLogger("execution_engine.audit")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(str(path), maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    ))
    log.addHandler(handler)
    return log


# ---------------------------------------------------------------------------
# Config + reports
# ---------------------------------------------------------------------------


OrderSide = Literal["BUY", "SELL"]
OrderStatus = Literal["NEW", "OPEN", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "ERROR"]


@dataclass
class ExecutionConfig:
    slippage_pct: float = 0.003                  # 0.30%
    retry_attempts: int = 3
    retry_backoff_initial_sec: float = 1.0
    retry_backoff_factor: float = 2.0
    order_timeout_sec: float = 60.0
    poll_interval_sec: float = 1.0
    log_path: str = "user_data/logs/execution.log"
    dry_run: bool = True
    # Auth — prefer the JSON key file Coinbase emits from the
    # CDP / Advanced Trade portal. The SDK loads it natively and
    # avoids us juggling a multi-line PEM private key inside .env.
    key_file_env: str = "COINBASE_KEY_FILE"
    api_key_env: str = "COINBASE_API_KEY"
    api_secret_env: str = "COINBASE_API_SECRET"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "ExecutionConfig":
        if not d:
            return cls()
        kwargs = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**kwargs)


@dataclass
class OrderReport:
    order_id: str
    client_order_id: str
    product_id: str
    side: OrderSide
    base_size: float
    limit_price: float
    signal_price: float
    status: OrderStatus
    filled_size: float = 0.0
    remaining_size: float = 0.0
    average_price: float | None = None
    attempts: int = 1
    cancelled_reason: str | None = None
    error: str | None = None
    submitted_at: float = field(default_factory=time.time)
    finalised_at: float | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def filled_pct(self) -> float:
        if self.base_size <= 0:
            return 0.0
        return min(1.0, max(0.0, self.filled_size / self.base_size))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class SlippageError(Exception):
    pass


class ExecutionEngine:
    """
    Thread-safe-ish (one ExecutionEngine per strategy run; the SDK calls
    are synchronous but we serialise post-place polling with a lock).
    """

    def __init__(
        self,
        config: ExecutionConfig | None = None,
        *,
        client: Any = None,
        price_fn: Callable[[str], tuple[float, float]] | None = None,
    ) -> None:
        """
        Args:
            client: Coinbase RESTClient instance — leave None to construct
                from env vars at first call (or in dry-run, never).
            price_fn: optional callable returning (best_bid, best_ask) for a
                product_id. Defaults to a SDK-backed implementation in live mode.
        """
        self.cfg = config or ExecutionConfig()
        self._client = client
        self._price_fn = price_fn
        self._lock = threading.Lock()
        self._audit = _setup_execution_logger(self.cfg.log_path)
        if self.cfg.dry_run:
            self._audit.info("INIT    dry_run=True (no network calls)")
        else:
            self._audit.info("INIT    dry_run=False (LIVE Coinbase Advanced Trade)")

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_config_file(cls, path: str | Path) -> "ExecutionEngine":
        cfg_dict = json.loads(Path(path).read_text())
        exec_cfg = ExecutionConfig.from_dict(cfg_dict.get("execution", {}))
        return cls(exec_cfg)

    def _ensure_client(self) -> Any:
        if self._client is not None or self.cfg.dry_run:
            return self._client
        try:
            from coinbase.rest import RESTClient
        except Exception as exc:
            raise RuntimeError(
                "coinbase-advanced-py SDK is required for live execution; "
                f"`pip install coinbase-advanced-py`. Original error: {exc}"
            ) from exc

        # Preferred path: JSON key file Coinbase downloads from the CDP /
        # Advanced Trade portal. Looks like:
        #   { "name": "organizations/.../apiKeys/...",
        #     "privateKey": "-----BEGIN EC PRIVATE KEY-----\n..." }
        key_file = os.environ.get(self.cfg.key_file_env, "").strip()
        if key_file:
            if not os.path.exists(key_file):
                raise RuntimeError(
                    f"Coinbase key file {key_file} (from {self.cfg.key_file_env}) "
                    f"does not exist."
                )
            self._client = RESTClient(key_file=key_file)
            return self._client

        # Fallback: env-var pair. The PEM private key needs literal newlines
        # so set it via `printf "%b\n" "$KEY"` or use a multi-line .env.
        api_key = os.environ.get(self.cfg.api_key_env, "").strip()
        api_secret = os.environ.get(self.cfg.api_secret_env, "").strip()
        if not api_key or not api_secret:
            raise RuntimeError(
                f"Coinbase credentials missing — set {self.cfg.key_file_env} "
                f"(recommended) or {self.cfg.api_key_env} / "
                f"{self.cfg.api_secret_env} env vars."
            )
        self._client = RESTClient(api_key=api_key, api_secret=api_secret)
        return self._client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def place_limit(
        self,
        product_id: str,
        side: OrderSide,
        base_size: float,
        limit_price: float,
        signal_price: float,
        *,
        client_order_id: str | None = None,
        post_only: bool = True,
        wait: bool = True,
    ) -> OrderReport:
        """
        Place a limit order with all the safety bells.

        Args:
            product_id: Coinbase product ID (e.g. "BTC-USD"; note dash, not slash)
            side: "BUY" or "SELL"
            base_size: amount of base asset
            limit_price: price to post
            signal_price: the model's reference price — slippage is computed
                relative to this, not to limit_price.
            client_order_id: optional idempotency key (one is generated otherwise)
            post_only: maker-only (avoids accidental crossing)
            wait: if True (default), block until FILLED / CANCELLED / timeout.
                  Set False for fire-and-forget; caller must call `monitor()`.
        """
        client_order_id = client_order_id or f"freqtft-{uuid.uuid4().hex[:12]}"
        report = OrderReport(
            order_id="",
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            base_size=float(base_size),
            limit_price=float(limit_price),
            signal_price=float(signal_price),
            status="NEW",
        )

        # --- Slippage gate -------------------------------------------------
        try:
            self._slippage_gate(product_id, side, signal_price)
        except SlippageError as exc:
            report.status = "REJECTED"
            report.cancelled_reason = str(exc)
            report.finalised_at = time.time()
            self._audit.warning(
                "REJECT  product=%s %s slippage_gate: %s",
                product_id, side, exc,
            )
            return report

        # --- Place with retry/backoff --------------------------------------
        place_resp: Any = None
        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.retry_attempts + 1):
            report.attempts = attempt
            try:
                place_resp = self._submit_order(
                    product_id=product_id, side=side, base_size=base_size,
                    limit_price=limit_price, client_order_id=client_order_id,
                    post_only=post_only,
                )
                break
            except Exception as exc:
                last_exc = exc
                wait_s = (
                    self.cfg.retry_backoff_initial_sec
                    * (self.cfg.retry_backoff_factor ** (attempt - 1))
                )
                self._audit.warning(
                    "RETRY   attempt=%d/%d wait=%.1fs err=%r",
                    attempt, self.cfg.retry_attempts, wait_s, exc,
                )
                if attempt < self.cfg.retry_attempts:
                    time.sleep(wait_s)

        if place_resp is None:
            report.status = "ERROR"
            report.error = repr(last_exc) if last_exc else "unknown"
            report.finalised_at = time.time()
            self._audit.error(
                "ERROR   client_oid=%s after %d attempts: %s",
                client_order_id, self.cfg.retry_attempts, report.error,
            )
            return report

        order_id = self._extract_order_id(place_resp, fallback=client_order_id)
        report.order_id = order_id
        report.status = "OPEN"
        self._audit.info(
            "PLACE   id=%s client_oid=%s %s %s %s @ %s",
            order_id, client_order_id, side, base_size, product_id, limit_price,
        )

        if not wait:
            return report

        # --- Monitor until filled / timeout --------------------------------
        return self.monitor(report)

    def monitor(self, report: OrderReport) -> OrderReport:
        """Poll until FILLED, fully cancelled, or timeout."""
        deadline = time.time() + self.cfg.order_timeout_sec
        while time.time() < deadline:
            with self._lock:
                state = self._fetch_order(report.order_id, report.client_order_id)

            if state is None:
                time.sleep(self.cfg.poll_interval_sec)
                continue

            filled = float(state.get("filled_size", 0.0))
            avg_price = state.get("average_filled_price")
            status = str(state.get("status", "")).upper()

            if filled > report.filled_size:
                # New partial — log it
                delta = filled - report.filled_size
                report.fills.append({
                    "ts": time.time(),
                    "delta": float(delta),
                    "cumulative": float(filled),
                    "avg_price": avg_price,
                })
                self._audit.info(
                    "FILL    id=%s filled=%.8f (%.1f%%) avg=%s",
                    report.order_id, filled, 100 * filled / max(report.base_size, 1e-12),
                    avg_price,
                )

            report.filled_size = filled
            report.remaining_size = max(0.0, report.base_size - filled)
            if avg_price is not None:
                try:
                    report.average_price = float(avg_price)
                except Exception:
                    pass

            if status in ("FILLED", "DONE") and report.remaining_size <= 0:
                report.status = "FILLED"
                report.finalised_at = time.time()
                self._audit.info(
                    "FILLED  id=%s total=%.8f avg=%s",
                    report.order_id, report.filled_size, report.average_price,
                )
                return report
            if status in ("CANCELLED", "EXPIRED", "REJECTED"):
                report.status = "PARTIAL" if report.filled_size > 0 else "CANCELLED"
                report.cancelled_reason = status
                report.finalised_at = time.time()
                self._audit.warning(
                    "CANCEL  id=%s reason=%s filled=%.8f",
                    report.order_id, status, report.filled_size,
                )
                return report

            time.sleep(self.cfg.poll_interval_sec)

        # Timed out — cancel
        try:
            self._cancel(report.order_id, report.client_order_id)
            report.cancelled_reason = "timeout"
            report.status = "PARTIAL" if report.filled_size > 0 else "CANCELLED"
            report.finalised_at = time.time()
            self._audit.warning(
                "TIMEOUT id=%s after %.1fs filled=%.8f → cancel",
                report.order_id, self.cfg.order_timeout_sec, report.filled_size,
            )
        except Exception as exc:
            report.error = f"cancel_failed: {exc!r}"
            self._audit.error(
                "CANCEL_FAIL id=%s err=%r", report.order_id, exc,
            )
        return report

    def cancel(self, order_id: str, client_order_id: str | None = None) -> bool:
        """Manually cancel an open order. Returns True on success."""
        try:
            self._cancel(order_id, client_order_id)
            self._audit.info("CANCEL_REQ id=%s", order_id)
            return True
        except Exception as exc:
            self._audit.error("CANCEL_FAIL id=%s err=%r", order_id, exc)
            return False

    # ------------------------------------------------------------------
    # SDK plumbing — separated so tests can inject a fake `client`.
    # ------------------------------------------------------------------

    def _slippage_gate(self, product_id: str, side: OrderSide, signal_price: float) -> None:
        """
        Compare the *current* best bid (for SELL) / best ask (for BUY) to the
        signal price. Refuse to place if drift > `slippage_pct`.
        """
        if self.cfg.slippage_pct <= 0:
            return
        try:
            bid, ask = self._best_prices(product_id)
        except Exception as exc:
            # Don't block on a price-fetch failure in dry mode; in live mode
            # we *do* refuse rather than place blind.
            if self.cfg.dry_run:
                return
            raise SlippageError(f"price fetch failed: {exc!r}") from exc
        ref = ask if side == "BUY" else bid
        if signal_price <= 0 or ref <= 0:
            raise SlippageError(f"invalid prices ref={ref} signal={signal_price}")
        drift = abs(ref - signal_price) / signal_price
        if drift > self.cfg.slippage_pct:
            raise SlippageError(
                f"drift {drift:.4%} > limit {self.cfg.slippage_pct:.4%} "
                f"(ref={ref}, signal={signal_price})"
            )

    def _best_prices(self, product_id: str) -> tuple[float, float]:
        """
        Top-of-book bid/ask used by the slippage gate.

        Uses `get_best_bid_ask(product_ids=[...])` — the dedicated CDP
        endpoint — instead of `get_product()`, which only exposes 24h
        stats and does not populate live bid/ask attributes.
        """
        if self._price_fn is not None:
            return self._price_fn(product_id)
        if self.cfg.dry_run:
            # Synthetic: tight spread around signal price isn't useful here;
            # in dry-run we never call this. Returning sentinels keeps tests
            # that route through us aware of the path.
            return (0.0, 0.0)
        client = self._ensure_client()
        resp = client.get_best_bid_ask(product_ids=[product_id])
        pricebooks = (
            getattr(resp, "pricebooks", None)
            or (resp["pricebooks"] if isinstance(resp, dict) and "pricebooks" in resp else [])
        )
        if not pricebooks:
            raise SlippageError(f"no pricebook for {product_id}")
        pb = pricebooks[0]
        bids = getattr(pb, "bids", None) or (pb.get("bids", []) if isinstance(pb, dict) else [])
        asks = getattr(pb, "asks", None) or (pb.get("asks", []) if isinstance(pb, dict) else [])
        if not bids or not asks:
            raise SlippageError(f"empty book for {product_id}")
        bid_p = float(getattr(bids[0], "price", None)
                      or (bids[0]["price"] if isinstance(bids[0], dict) else 0))
        ask_p = float(getattr(asks[0], "price", None)
                      or (asks[0]["price"] if isinstance(asks[0], dict) else 0))
        return bid_p, ask_p

    def _submit_order(
        self, *, product_id: str, side: OrderSide, base_size: float,
        limit_price: float, client_order_id: str, post_only: bool,
    ) -> Any:
        if self.cfg.dry_run:
            time.sleep(0.001)
            # Deterministic-ish synthetic ID
            return {
                "success": True,
                "order_id": f"dry-{client_order_id}-{int(time.time()*1000)}",
                "client_order_id": client_order_id,
            }
        client = self._ensure_client()
        # The SDK exposes side-specific helpers; we use them directly.
        if side == "BUY":
            return client.limit_order_gtc_buy(
                client_order_id=client_order_id,
                product_id=product_id,
                base_size=str(base_size),
                limit_price=str(limit_price),
                post_only=post_only,
            )
        return client.limit_order_gtc_sell(
            client_order_id=client_order_id,
            product_id=product_id,
            base_size=str(base_size),
            limit_price=str(limit_price),
            post_only=post_only,
        )

    def _fetch_order(
        self, order_id: str, client_order_id: str | None,
    ) -> dict[str, Any] | None:
        if self.cfg.dry_run:
            # Simulate fast fill in dry-run
            return {"status": "FILLED", "filled_size": "_DRY_RUN_FULL_", "average_filled_price": None}
        client = self._ensure_client()
        try:
            resp = client.get_order(order_id=order_id)
            order = (
                getattr(resp, "order", None)
                or (resp["order"] if isinstance(resp, dict) and "order" in resp else resp)
            )
            return {
                "status": (
                    getattr(order, "status", None)
                    or (order["status"] if isinstance(order, dict) and "status" in order else "")
                ),
                "filled_size": float(
                    getattr(order, "filled_size", 0)
                    or (order.get("filled_size", 0) if isinstance(order, dict) else 0)
                ),
                "average_filled_price": (
                    getattr(order, "average_filled_price", None)
                    or (order.get("average_filled_price") if isinstance(order, dict) else None)
                ),
            }
        except Exception:
            return None

    def _cancel(self, order_id: str, client_order_id: str | None) -> None:
        if self.cfg.dry_run:
            return
        client = self._ensure_client()
        client.cancel_orders(order_ids=[order_id])

    @staticmethod
    def _extract_order_id(resp: Any, *, fallback: str) -> str:
        # SDK responses are pydantic objects, dicts, or both; tolerate all.
        for getter in (
            lambda r: getattr(r, "success_response", None),
            lambda r: r.get("success_response") if isinstance(r, dict) else None,
        ):
            sr = getter(resp)
            if sr:
                oid = getattr(sr, "order_id", None) or (
                    sr.get("order_id") if isinstance(sr, dict) else None
                )
                if oid:
                    return str(oid)
        for key in ("order_id", "id"):
            v = (
                getattr(resp, key, None)
                or (resp[key] if isinstance(resp, dict) and key in resp else None)
            )
            if v:
                return str(v)
        return fallback

    # ------------------------------------------------------------------
    # Override hook for monitor() so dry-run can simulate fills.
    # ------------------------------------------------------------------


class DryRunExecutionEngine(ExecutionEngine):
    """
    Deterministic dry-run engine for tests. Behaviour:
      - place_limit always succeeds at attempt 1.
      - Slippage gate uses signal_price + a configurable mock drift.
      - _fetch_order simulates a fill timeline driven by `_fill_schedule`.
    """

    def __init__(
        self,
        config: ExecutionConfig | None = None,
        *,
        mock_drift_pct: float = 0.0,
        fill_after_polls: int = 1,
        partial_fills: int = 0,
        order_will_be_cancelled: bool = False,
    ) -> None:
        cfg = config or ExecutionConfig(dry_run=True, poll_interval_sec=0.0)
        cfg.dry_run = True
        super().__init__(cfg)
        self._mock_drift_pct = mock_drift_pct
        self._fill_after_polls = max(1, fill_after_polls)
        self._partial_fills = max(0, partial_fills)
        self._poll_count: dict[str, int] = {}
        self._cancel_requested: set[str] = set()
        self._will_cancel = order_will_be_cancelled

    def _slippage_gate(self, product_id, side, signal_price):
        if abs(self._mock_drift_pct) > self.cfg.slippage_pct:
            raise SlippageError(
                f"drift {abs(self._mock_drift_pct):.4%} > limit "
                f"{self.cfg.slippage_pct:.4%} (mock)"
            )

    def _fetch_order(self, order_id, client_order_id):
        n = self._poll_count.get(order_id, 0) + 1
        self._poll_count[order_id] = n
        if order_id in self._cancel_requested or self._will_cancel:
            return {"status": "CANCELLED", "filled_size": 0.0, "average_filled_price": None}
        if n < self._fill_after_polls:
            # Emit partial fills along the way
            if self._partial_fills > 0 and n >= 1:
                # Fill `n / fill_after_polls` proportionally (capped to base size 1.0
                # for the purposes of dry-run; the real base_size is tracked by report)
                progress = min(0.99, n / self._fill_after_polls)
                return {
                    "status": "OPEN",
                    "filled_size": progress,
                    "average_filled_price": 1.0,
                }
            return {"status": "OPEN", "filled_size": 0.0, "average_filled_price": None}
        return {"status": "FILLED", "filled_size": 1.0, "average_filled_price": 1.0}

    def _cancel(self, order_id, client_order_id):
        self._cancel_requested.add(order_id)


# ---------------------------------------------------------------------------
# Convenience: pair "BTC/USD" → product_id "BTC-USD"
# ---------------------------------------------------------------------------


def pair_to_product_id(pair: str) -> str:
    """Convert Freqtrade pair format to Coinbase product_id."""
    return pair.replace("/", "-").upper()
