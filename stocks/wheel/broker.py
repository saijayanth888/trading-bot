"""
wheel.broker — thin Alpaca client wrapper.

Wraps alpaca-py with the methods the wheel needs:
    get_account()
    get_stock_quote(symbol)
    list_options(symbol, expiry_window, type)  → returns OptionContract list
    quote_option(option_symbol)                → fills bid/ask/IV/delta
    sell_to_open(option_symbol, qty, limit)
    buy_to_close(option_symbol, qty, limit)
    sell_shares(symbol, qty, limit)
    has_pending_orders(symbol)
    cancel_stale_orders()

Every call funnels through here so the rest of the wheel module is
broker-agnostic and trivially mockable in tests.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderClass,
    OrderSide,
    OrderType,
    PositionIntent,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionLatestQuoteRequest,
    OptionSnapshotRequest,
    StockBarsRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .strategy import OptionContract

logger = logging.getLogger(__name__)


@dataclass
class AccountSnapshot:
    cash: float
    buying_power: float
    portfolio_value: float
    paper: bool
    # Alpaca enforces CSP submits against `options_buying_power`, NOT
    # `buying_power` (which is Reg-T margin BP, ~2× cash). They diverge
    # significantly once any orders are pending or any margin is in use —
    # the 2026-05-13 wheel_sell_csps failures hit this exactly.
    options_buying_power: float = 0.0


class Broker:
    """Wheel-side wrapper around alpaca-py's TradingClient + data clients."""

    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.paper = paper
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.stock_data = StockHistoricalDataClient(api_key, secret_key)
        self.option_data = OptionHistoricalDataClient(api_key, secret_key)

    # ── Account ────────────────────────────────────────────────────────────

    def get_account(self) -> AccountSnapshot:
        a = self.trading.get_account()
        return AccountSnapshot(
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            portfolio_value=float(a.portfolio_value),
            paper=self.paper,
            options_buying_power=float(getattr(a, "options_buying_power", None) or 0.0),
        )

    # ── Stock data ─────────────────────────────────────────────────────────

    def get_stock_price(self, symbol: str) -> float:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp = self.stock_data.get_stock_latest_trade(req)
        trade = resp[symbol]
        return float(trade.price)

    def get_stock_bars(
        self,
        symbol: str,
        timeframe: str = "5Min",
        limit: int = 288,
    ) -> List[dict]:
        """Return OHLCV bars in the shape Lightweight-Charts expects.

        timeframe: "1Min", "5Min", "15Min", "1Hour", "1Day"
        limit: number of bars (default 288 = 24h of 5-min bars)
        Returns: [{ "time": unix_seconds, "open": .., "high": .., "low": ..,
                    "close": .., "volume": .. }, ...]  oldest → newest
        """
        # Map timeframe string → SDK TimeFrame
        unit_map = {
            "min": TimeFrameUnit.Minute,
            "hour": TimeFrameUnit.Hour,
            "day": TimeFrameUnit.Day,
        }
        tf_lc = timeframe.lower()
        if tf_lc.endswith("min"):
            amount = int(tf_lc[:-3]) if tf_lc[:-3] else 1
            tf = TimeFrame(amount, TimeFrameUnit.Minute)
        elif tf_lc.endswith("hour"):
            amount = int(tf_lc[:-4]) if tf_lc[:-4] else 1
            tf = TimeFrame(amount, TimeFrameUnit.Hour)
        elif tf_lc in ("1day", "day", "1d"):
            tf = TimeFrame(1, TimeFrameUnit.Day)
        else:
            raise ValueError(f"unsupported timeframe: {timeframe}")

        # Wide lookback to absorb non-trading hours and weekends. Floor at
        # 7 days so 1Min×limit-bars-on-a-weekend still hits Friday's bars.
        # We always trim to `limit` after fetching.
        end = datetime.now(timezone.utc)
        if tf.unit == TimeFrameUnit.Minute:
            wanted = timedelta(minutes=tf.amount * limit * 7)
            start = end - max(wanted, timedelta(days=7))
        elif tf.unit == TimeFrameUnit.Hour:
            wanted = timedelta(hours=tf.amount * limit * 3)
            start = end - max(wanted, timedelta(days=7))
        else:
            start = end - timedelta(days=int(tf.amount * limit * 1.6))

        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            feed=os.environ.get("ALPACA_DATA_FEED", "iex"),
        )
        resp = self.stock_data.get_stock_bars(req)
        bars_obj = resp.data.get(symbol, []) if hasattr(resp, "data") else []
        out: List[dict] = []
        for b in bars_obj:
            ts = b.timestamp
            if hasattr(ts, "timestamp"):
                t = int(ts.timestamp())
            else:
                t = int(ts)
            out.append({
                "time": t,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": int(getattr(b, "volume", 0) or 0),
            })
        # Trim to last `limit`
        return out[-limit:]

    # ── Options chain ──────────────────────────────────────────────────────

    def list_put_contracts(
        self,
        underlying: str,
        min_dte: int,
        max_dte: int,
        strike_pct_band: tuple[float, float] = (0.85, 0.98),
    ) -> List[OptionContract]:
        """Return puts in the strike band % of current spot, in DTE window."""
        spot = self.get_stock_price(underlying)
        today = date.today()
        gte = today + timedelta(days=min_dte)
        lte = today + timedelta(days=max_dte)
        contracts_resp = self.trading.get_option_contracts(
            GetOptionContractsRequest(
                underlying_symbols=[underlying],
                status=AssetStatus.ACTIVE,
                expiration_date_gte=gte,
                expiration_date_lte=lte,
                type=ContractType.PUT,
                strike_price_gte=str(spot * strike_pct_band[0]),
                strike_price_lte=str(spot * strike_pct_band[1]),
            )
        )
        # The API returns a paginated `OptionContractsResponse` with
        # `option_contracts: list[OptionContract]`.
        raw = getattr(contracts_resp, "option_contracts", None) or []
        if not raw:
            return []

        # Snapshot each one — gives us BOTH quote (bid/ask) AND greeks (delta).
        # Snapshots is the right call here; get_option_latest_quote alone
        # doesn't return delta, and the contract object's `delta` field is
        # always 0 in the trading client's response.
        symbols = [c.symbol for c in raw]
        snapshots = self.option_data.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=symbols)
        )
        out: List[OptionContract] = []
        for c in raw:
            snap = snapshots.get(c.symbol)
            if snap is None:
                continue
            q = getattr(snap, "latest_quote", None)
            greeks = getattr(snap, "greeks", None)
            delta = float(getattr(greeks, "delta", 0.0) or 0.0) if greeks else 0.0
            bid = float(getattr(q, "bid_price", 0.0) or 0.0) if q else 0.0
            ask = float(getattr(q, "ask_price", 0.0) or 0.0) if q else 0.0
            out.append(OptionContract(
                symbol=c.symbol,
                underlying=underlying,
                strike=float(c.strike_price),
                expiry=c.expiration_date,
                contract_type="put",
                delta=delta,
                bid=bid,
                ask=ask,
                open_interest=int(getattr(c, "open_interest", 0) or 0),
            ))
        return out

    def list_call_contracts(
        self,
        underlying: str,
        min_dte: int,
        max_dte: int,
        min_strike: float,
        strike_pct_above_spot: float = 1.10,
    ) -> List[OptionContract]:
        """Return calls strike >= min_strike (cost basis), within DTE window."""
        spot = self.get_stock_price(underlying)
        today = date.today()
        contracts_resp = self.trading.get_option_contracts(
            GetOptionContractsRequest(
                underlying_symbols=[underlying],
                status=AssetStatus.ACTIVE,
                expiration_date_gte=today + timedelta(days=min_dte),
                expiration_date_lte=today + timedelta(days=max_dte),
                type=ContractType.CALL,
                strike_price_gte=str(min_strike),
                strike_price_lte=str(spot * strike_pct_above_spot),
            )
        )
        raw = getattr(contracts_resp, "option_contracts", None) or []
        if not raw:
            return []
        symbols = [c.symbol for c in raw]
        snapshots = self.option_data.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=symbols)
        )
        out: List[OptionContract] = []
        for c in raw:
            snap = snapshots.get(c.symbol)
            if snap is None:
                continue
            q = getattr(snap, "latest_quote", None)
            greeks = getattr(snap, "greeks", None)
            delta = float(getattr(greeks, "delta", 0.0) or 0.0) if greeks else 0.0
            bid = float(getattr(q, "bid_price", 0.0) or 0.0) if q else 0.0
            ask = float(getattr(q, "ask_price", 0.0) or 0.0) if q else 0.0
            out.append(OptionContract(
                symbol=c.symbol,
                underlying=underlying,
                strike=float(c.strike_price),
                expiry=c.expiration_date,
                contract_type="call",
                delta=delta,
                bid=bid,
                ask=ask,
                open_interest=int(getattr(c, "open_interest", 0) or 0),
            ))
        return out

    # ── Order placement ────────────────────────────────────────────────────

    def sell_to_open(self, option_symbol: str, qty: int, limit_price: float) -> dict:
        """Sell-to-open an option as a sell limit order."""
        order = LimitOrderRequest(
            symbol=option_symbol,
            qty=qty,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            limit_price=round(limit_price, 2),
            time_in_force=TimeInForce.DAY,
            position_intent=PositionIntent.SELL_TO_OPEN,
        )
        resp = self.trading.submit_order(order)
        logger.info(
            "STO %s qty=%d limit=$%.2f → order_id=%s",
            option_symbol, qty, limit_price, getattr(resp, "id", "?"),
        )
        return {"id": getattr(resp, "id", None), "status": str(getattr(resp, "status", ""))}

    def buy_to_close(self, option_symbol: str, qty: int, limit_price: float) -> dict:
        order = LimitOrderRequest(
            symbol=option_symbol,
            qty=qty,
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            limit_price=round(limit_price, 2),
            time_in_force=TimeInForce.DAY,
            position_intent=PositionIntent.BUY_TO_CLOSE,
        )
        resp = self.trading.submit_order(order)
        logger.info(
            "BTC %s qty=%d limit=$%.2f → order_id=%s",
            option_symbol, qty, limit_price, getattr(resp, "id", "?"),
        )
        return {"id": getattr(resp, "id", None), "status": str(getattr(resp, "status", ""))}

    # ── Position queries (used for assignment detection) ───────────────────

    def get_option_position_qty(self, option_symbol: str) -> int:
        """Return the broker-side quantity for one option symbol.

        Convention: short positions return a NEGATIVE quantity; long positions
        POSITIVE; the symbol-not-found case returns 0 (which is also what the
        broker reports after an option is assigned away).

        Used by wheel.runner.assignment_check() to detect when a short put
        position has gone to zero qty at the broker. We pair that with a
        matching long-shares position to confirm assignment vs ordinary close.
        """
        try:
            pos = self.trading.get_open_position(option_symbol)
            return int(float(getattr(pos, "qty", 0) or 0))
        except Exception as exc:
            # alpaca-py raises APIError("position does not exist") on flat
            msg = str(exc).lower()
            if "position does not exist" in msg or "not found" in msg or "404" in msg:
                return 0
            logger.warning("get_option_position_qty(%s) failed: %s", option_symbol, exc)
            return 0

    def get_stock_position_qty(self, underlying: str) -> int:
        """Return the broker-side share quantity for one underlying."""
        try:
            pos = self.trading.get_open_position(underlying)
            return int(float(getattr(pos, "qty", 0) or 0))
        except Exception as exc:
            msg = str(exc).lower()
            if "position does not exist" in msg or "not found" in msg or "404" in msg:
                return 0
            logger.warning("get_stock_position_qty(%s) failed: %s", underlying, exc)
            return 0

    # ── Order maintenance ──────────────────────────────────────────────────

    def list_open_orders(self, symbol: Optional[str] = None) -> list:
        # Use the SDK enum (P1-S6). String "open" worked because QueryOrderStatus
        # is a str-enum, but future SDK versions may tighten validation.
        req = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol] if symbol else None,
        )
        return self.trading.get_orders(req)

    def cancel_stale_orders(self, max_age_minutes: int = 240) -> int:
        """Cancel any DAY-tif orders older than max_age_minutes. Returns count."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        n = 0
        for o in self.list_open_orders():
            ts = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
            if ts and ts < cutoff:
                self.trading.cancel_order_by_id(o.id)
                n += 1
        return n


def from_env() -> Broker:
    """Build a Broker from the unified .env (loaded by shark.run on import)."""
    key = os.environ.get("ALPACA_API_KEY")
    sec = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not sec:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY missing — populate trading-bot/.env"
        )
    paper = os.environ.get("TRADING_MODE", "paper").lower() == "paper"
    return Broker(key, sec, paper=paper)
