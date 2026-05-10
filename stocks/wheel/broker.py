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
    StockLatestTradeRequest,
)

from .strategy import OptionContract

logger = logging.getLogger(__name__)


@dataclass
class AccountSnapshot:
    cash: float
    buying_power: float
    portfolio_value: float
    paper: bool


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
        )

    # ── Stock data ─────────────────────────────────────────────────────────

    def get_stock_price(self, symbol: str) -> float:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp = self.stock_data.get_stock_latest_trade(req)
        trade = resp[symbol]
        return float(trade.price)

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

    # ── Order maintenance ──────────────────────────────────────────────────

    def list_open_orders(self, symbol: Optional[str] = None) -> list:
        req = GetOrdersRequest(status="open", symbols=[symbol] if symbol else None)
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
