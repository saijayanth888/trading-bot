"""
wheel.runner — orchestrates per-cycle wheel actions.

Three runner entry points, each one-shot for cron firing:

    sell_csps()          Friday 11 AM ET — sell new puts on allowed tickers
    profit_take_check()  Mon-Fri 10/14 ET — buy-to-close any short puts that
                         have decayed to profit-take threshold
    sell_covered_calls() Monday 11 AM ET — for each held assignment, sell a
                         covered call >= cost basis

All three return None on success and log loudly on failure. They never raise
beyond unrecoverable misconfiguration; transient broker failures are logged
and the next firing retries.

Reusable safety:
    * shark.memory.kill_switch — shared kill-flag; if memory/KILL.flag exists,
      no new positions are opened. (Wheel respects shark's kill switch and
      adds its own per-ticker kill flags via wheel.state.is_killed().)
    * pre-flight account snapshot — won't enter if cash buffer breaches floor
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

# Path so `python -m wheel.runner` works regardless of cwd
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load unified .env via shark's loader (no-op if already loaded)
import shark.run  # noqa: F401

from shark.memory.kill_switch import is_killed as _shark_kill_active

from .broker import from_env
from .config import load_config
from .state import (
    Position, TradeRecord,
    add_position, append_trade, find_open_csp, find_open_cc,
    is_killed, kill_ticker, load_positions, now_iso, remove_position,
    shares_held, update_position,
)
from .strategy import (
    OptionContract,
    filter_calls,
    filter_puts,
    profit_take_threshold,
    select_best,
)


logger = logging.getLogger(__name__)


def _fetch_spy_regime(timeout_s: float = 2.0) -> str:
    """Pull the current SPY regime from the dashboard ops API.

    Returns "unknown" on any error so the regime_gating defaults
    (which treat unknown as a no-op) keep entries flowing safely.
    Used by sell_csps() to apply the WheelConfig.regime_gating policy.
    """
    import os, urllib.request, json as _json
    base = os.environ.get("DASHBOARD_INTERNAL_URL", "http://localhost:8081")
    try:
        with urllib.request.urlopen(f"{base}/api/ops/stock_regime", timeout=timeout_s) as r:
            d = _json.loads(r.read().decode()).get("data") or {}
            return str(d.get("current") or "unknown")
    except Exception as exc:
        logger.warning("wheel: SPY regime fetch failed (%s) — defaulting to 'unknown'", exc)
        return "unknown"


# ── Entry: sell_csps ────────────────────────────────────────────────────────


def sell_csps(symbols_override: Optional[List[str]] = None) -> dict:
    """Sell cash-secured puts for each allowed ticker. One-shot.

    Applies the configured regime_gating policy: SPY regime is fetched
    from the dashboard and used to either hard-block new CSPs (e.g. in
    trending_down) or shift the delta band (e.g. tighter in high_volatility).
    See WheelConfig.regime_gating for the default policy.

    Returns a summary dict suitable for Telegram delivery.
    """
    cfg = load_config()
    summary = {"phase": "sell_csps", "actions": [], "skipped": [], "errors": []}

    if _shark_kill_active():
        logger.warning("Shark kill switch active — sell_csps() aborted")
        summary["errors"].append("shark kill switch active")
        return summary

    # Regime gate — hard-block whole-cycle entries if SPY says risk-off.
    regime = _fetch_spy_regime()
    rg_policy = (cfg.regime_gating or {}).get(regime, {})
    if rg_policy.get("block"):
        logger.warning("wheel: SPY regime=%s blocks new CSP entries (policy)", regime)
        summary["skipped"].append(f"regime_gate: SPY={regime} blocks new CSPs")
        summary["regime"] = regime
        summary["regime_blocked"] = True
        return summary
    summary["regime"] = regime
    summary["delta_max_shift"] = float(rg_policy.get("delta_max_shift", 0.0))

    # Apply per-regime delta shift to the cfg used by the selector below.
    # Negative shift = tighter / further-OTM; positive = looser.
    if summary["delta_max_shift"] != 0.0:
        from dataclasses import replace
        cfg = replace(cfg, delta_max=max(
            cfg.delta_min + 0.01,
            min(0.99, cfg.delta_max + summary["delta_max_shift"]),
        ))
        logger.info(
            "wheel: regime=%s — delta_max adjusted %+.2f → %.2f",
            regime, summary["delta_max_shift"], cfg.delta_max,
        )

    broker = from_env()
    acct = broker.get_account()
    logger.info(
        "account: cash=$%.2f buying_power=$%.2f portfolio=$%.2f paper=%s",
        acct.cash, acct.buying_power, acct.portfolio_value, acct.paper,
    )

    symbols = symbols_override or list(cfg.symbols)
    for sym in symbols:
        try:
            _try_sell_csp(broker, sym, cfg, acct, summary)
        except Exception as exc:
            logger.exception("sell_csp(%s) crashed", sym)
            summary["errors"].append(f"{sym}: {exc!s}")
    return summary


def _try_sell_csp(broker, sym: str, cfg, acct, summary: dict) -> None:
    if is_killed(sym):
        summary["skipped"].append(f"{sym}: per-ticker kill flag active")
        return

    if find_open_csp(sym) is not None:
        summary["skipped"].append(f"{sym}: already have an open CSP")
        return

    if shares_held(sym) > 0:
        summary["skipped"].append(f"{sym}: holding shares — covered-call leg, not CSP")
        return

    contracts = broker.list_put_contracts(
        underlying=sym,
        min_dte=cfg.dte_min,
        max_dte=cfg.dte_max,
    )
    candidates = filter_puts(contracts, cfg)
    if not candidates:
        summary["skipped"].append(f"{sym}: no put passes the filter")
        return

    best_list = select_best(candidates, n=1)
    if not best_list:
        summary["skipped"].append(f"{sym}: select_best returned empty")
        return
    best: OptionContract = best_list[0]

    collateral = best.strike * 100  # 1 contract = 100 shares
    if collateral > cfg.max_risk_per_ticker_usd:
        summary["skipped"].append(
            f"{sym}: collateral ${collateral:.0f} > max_risk_per_ticker ${cfg.max_risk_per_ticker_usd:.0f}"
        )
        return
    if collateral > acct.buying_power:
        summary["skipped"].append(
            f"{sym}: collateral ${collateral:.0f} > buying_power ${acct.buying_power:.0f}"
        )
        return

    limit_price = best.mid or best.bid
    if limit_price <= 0:
        summary["skipped"].append(f"{sym}: zero quote (illiquid)")
        return

    order = broker.sell_to_open(best.symbol, qty=1, limit_price=limit_price)
    add_position(Position(
        underlying=sym,
        contract_symbol=best.symbol,
        kind="short_put",
        qty=1,
        strike=best.strike,
        expiry=best.expiry.isoformat() if best.expiry else None,
        entry_credit=limit_price * 100,  # USD
        opened_at=now_iso(),
    ))
    summary["actions"].append({
        "underlying": sym,
        "action": "sell_to_open_put",
        "symbol": best.symbol,
        "strike": best.strike,
        "dte": best.dte,
        "delta": round(best.delta, 3),
        "credit_usd": round(limit_price * 100, 2),
        "order_id": order.get("id"),
    })


# ── Entry: assignment_check ────────────────────────────────────────────────


def assignment_check(broker=None, positions: Optional[List[Position]] = None) -> dict:
    """Detect short_put → long_shares assignment and bridge the wheel cycle.

    For each open short_put position in the journal:
      1. Query the broker for the option's qty. If it is non-zero (still open),
         the position has NOT been assigned; skip.
      2. Query the broker for the underlying's share qty. If it equals
         100 * contracts (after subtracting any pre-existing shares we tracked),
         this short put was assigned: write a `long_shares` Position with
         entry_price=strike, source="wheel_assignment", and mark the short_put
         as status="assigned" (kept on file for audit; sell_covered_calls only
         consults `kind == "long_shares"`).
      3. If the option went to zero but no matching shares appeared, this was
         either an ordinary buy-to-close (handled by profit_take_check) or an
         externally cancelled position. Drop the stale row to keep state clean.

    Args:
        broker: Optional injected Broker (test seam). Defaults to from_env().
        positions: Optional injected positions list (test seam).

    Returns: same summary shape as the other entries.
    """
    summary = {"phase": "assignment_check", "actions": [], "skipped": [], "errors": []}
    open_positions = positions if positions is not None else load_positions()
    open_csps = [p for p in open_positions if p.kind == "short_put"]

    if not open_csps:
        summary["skipped"].append("no open CSPs to check for assignment")
        return summary

    if broker is None:
        broker = from_env()

    for pos in open_csps:
        try:
            _check_one_assignment(broker, pos, open_positions, summary)
        except Exception as exc:
            logger.exception("assignment_check(%s) crashed", pos.contract_symbol)
            summary["errors"].append(f"{pos.contract_symbol}: {exc!s}")
    return summary


def _check_one_assignment(
    broker,
    pos: Position,
    all_positions: List[Position],
    summary: dict,
) -> None:
    """Inspect one short_put position for assignment evidence."""
    # 1. Option still open at broker? Then nothing happened yet.
    opt_qty = broker.get_option_position_qty(pos.contract_symbol)
    if opt_qty != 0:
        summary["skipped"].append(
            f"{pos.contract_symbol}: option qty={opt_qty} (still open)"
        )
        return

    # 2. Option went flat. Look at the share side to disambiguate.
    expected_assigned_shares = 100 * pos.qty
    broker_shares = broker.get_stock_position_qty(pos.underlying)
    # How many shares does our journal already attribute to wheel positions
    # on this underlying? (Could be from prior assignments still rolling.)
    journal_shares = sum(
        p.qty for p in all_positions
        if p.kind == "long_shares" and p.underlying == pos.underlying
    )
    new_shares = broker_shares - journal_shares

    if new_shares >= expected_assigned_shares:
        # ASSIGNMENT confirmed: write the long_shares row, mark short_put assigned.
        add_position(Position(
            underlying=pos.underlying,
            contract_symbol="",  # shares have no contract symbol
            kind="long_shares",
            qty=expected_assigned_shares,
            strike=0.0,
            expiry=None,
            entry_credit=0.0,
            entry_price=pos.strike,
            opened_at=now_iso(),
            source="wheel_assignment",
        ))
        update_position(pos.contract_symbol, status="assigned")
        append_trade(TradeRecord(
            timestamp=now_iso(),
            underlying=pos.underlying,
            cycle="csp_assigned",
            pnl=0.0,  # premium retained; share P&L realized at call-away
            notes=(
                f"assigned: {pos.qty} put(s) @ strike ${pos.strike:.2f} → "
                f"{expected_assigned_shares} shares; credit ${pos.entry_credit:.2f} retained"
            ),
        ))
        summary["actions"].append({
            "underlying": pos.underlying,
            "action": "assignment_detected",
            "symbol": pos.contract_symbol,
            "strike": pos.strike,
            "shares_acquired": expected_assigned_shares,
            "credit_retained_usd": round(pos.entry_credit, 2),
        })
        logger.warning(
            "WHEEL ASSIGNMENT %s: %d short put(s) @ $%.2f → %d shares; cycle continues with CC",
            pos.underlying, pos.qty, pos.strike, expected_assigned_shares,
        )
        return

    # 3. Option flat but no matching shares — stale row from external close.
    logger.info(
        "wheel: %s option flat at broker but no matching shares "
        "(broker=%d, journal=%d) — removing stale row",
        pos.contract_symbol, broker_shares, journal_shares,
    )
    remove_position(pos.contract_symbol)
    summary["actions"].append({
        "underlying": pos.underlying,
        "action": "stale_csp_removed",
        "symbol": pos.contract_symbol,
        "reason": "option flat at broker but no matching shares — closed externally",
    })


# ── Entry: profit_take_check ────────────────────────────────────────────────


def profit_take_check() -> dict:
    """Walk open short puts; buy-to-close any whose mid is at or below the
    profit-take threshold. One-shot.

    Also pre-runs assignment_check() to detect short puts that have already
    been assigned to shares — this is what bridges the put-leg → covered-call
    leg of the wheel and was the missing piece in pilot v1.
    """
    cfg = load_config()
    summary = {"phase": "profit_take_check", "actions": [], "skipped": [], "errors": []}

    broker = from_env()

    # ── Assignment bridge: turn any flat-at-broker short_put into long_shares
    #    so sell_covered_calls() can pick it up on its next firing.
    try:
        ac_summary = assignment_check(broker=broker)
        for action in ac_summary.get("actions", []):
            summary["actions"].append(action)
        for skipped in ac_summary.get("skipped", []):
            # Don't pollute the main summary with noisy "still open" lines.
            if "still open" not in skipped:
                summary["skipped"].append(f"assignment_check: {skipped}")
        for err in ac_summary.get("errors", []):
            summary["errors"].append(f"assignment_check: {err}")
    except Exception as exc:
        logger.exception("assignment_check crashed inside profit_take_check")
        summary["errors"].append(f"assignment_check: {exc!s}")

    # Reload after potential assignment-driven mutations.
    open_csps = [p for p in load_positions() if p.kind == "short_put" and p.status != "assigned"]
    if not open_csps:
        summary["skipped"].append("no open CSPs")
        return summary

    for pos in open_csps:
        try:
            _check_csp_profit_take(broker, pos, cfg, summary)
        except Exception as exc:
            logger.exception("profit_take(%s) crashed", pos.contract_symbol)
            summary["errors"].append(f"{pos.contract_symbol}: {exc!s}")
    return summary


def _check_csp_profit_take(broker, pos: Position, cfg, summary: dict) -> None:
    """For one open CSP, check if it's hit profit-take or needs to roll."""
    contracts = broker.list_put_contracts(
        underlying=pos.underlying,
        min_dte=0,
        max_dte=60,
    )
    quote = next((c for c in contracts if c.symbol == pos.contract_symbol), None)
    if quote is None:
        summary["skipped"].append(f"{pos.contract_symbol}: not in chain (expired?)")
        return

    # We sold for entry_credit_per_contract = entry_credit / 100 / qty
    credit_per_share = pos.entry_credit / 100 / max(1, pos.qty)
    threshold = profit_take_threshold(credit_per_share, cfg)
    if quote.mid <= threshold:
        broker.buy_to_close(pos.contract_symbol, qty=pos.qty, limit_price=quote.mid)
        pnl_usd = (credit_per_share - quote.mid) * 100 * pos.qty
        append_trade(TradeRecord(
            timestamp=now_iso(),
            underlying=pos.underlying,
            cycle="csp_close",
            pnl=round(pnl_usd, 2),
            notes=f"closed at ${quote.mid:.2f} (entry ${credit_per_share:.2f}, threshold ${threshold:.2f})",
        ))
        remove_position(pos.contract_symbol)
        summary["actions"].append({
            "underlying": pos.underlying,
            "action": "buy_to_close_profit_take",
            "symbol": pos.contract_symbol,
            "exit_price": quote.mid,
            "pnl_usd": round(pnl_usd, 2),
        })
    elif abs(quote.delta) >= cfg.delta_roll_trigger:
        # Don't auto-roll in pilot — flag for operator
        summary["actions"].append({
            "underlying": pos.underlying,
            "action": "needs_roll",
            "symbol": pos.contract_symbol,
            "current_delta": round(quote.delta, 3),
            "trigger": cfg.delta_roll_trigger,
        })


# ── Entry: sell_covered_calls ──────────────────────────────────────────────


def sell_covered_calls() -> dict:
    """For each underlying where we hold ≥100 shares (assigned), sell a CC."""
    cfg = load_config()
    summary = {"phase": "sell_covered_calls", "actions": [], "skipped": [], "errors": []}
    if _shark_kill_active():
        summary["errors"].append("shark kill switch active")
        return summary

    broker = from_env()
    acct = broker.get_account()

    for sym in cfg.symbols:
        try:
            shares = shares_held(sym)
            if shares < 100:
                summary["skipped"].append(f"{sym}: only {shares} shares (need 100)")
                continue
            if find_open_cc(sym) is not None:
                summary["skipped"].append(f"{sym}: already have an open CC")
                continue

            # Cost basis = entry_price of the long_shares row (set on assignment)
            position_rows = [
                p for p in load_positions()
                if p.kind == "long_shares" and p.underlying == sym
            ]
            cost_basis = (
                sum(p.entry_price * p.qty for p in position_rows)
                / max(1, sum(p.qty for p in position_rows))
            )

            contracts = broker.list_call_contracts(
                underlying=sym,
                min_dte=cfg.dte_min,
                max_dte=cfg.dte_max,
                min_strike=cost_basis,
            )
            candidates = filter_calls(contracts, cfg, cost_basis=cost_basis)
            if not candidates:
                summary["skipped"].append(f"{sym}: no call passes the filter")
                continue

            best = select_best(candidates, n=1)[0]
            limit = best.mid or best.bid
            if limit <= 0:
                summary["skipped"].append(f"{sym}: zero quote on best CC")
                continue

            qty = shares // 100
            order = broker.sell_to_open(best.symbol, qty=qty, limit_price=limit)
            add_position(Position(
                underlying=sym,
                contract_symbol=best.symbol,
                kind="short_call",
                qty=qty,
                strike=best.strike,
                expiry=best.expiry.isoformat() if best.expiry else None,
                entry_credit=limit * 100 * qty,
                opened_at=now_iso(),
            ))
            summary["actions"].append({
                "underlying": sym,
                "action": "sell_to_open_call",
                "symbol": best.symbol,
                "strike": best.strike,
                "qty": qty,
                "credit_usd": round(limit * 100 * qty, 2),
                "order_id": order.get("id"),
            })
        except Exception as exc:
            logger.exception("sell_cc(%s) crashed", sym)
            summary["errors"].append(f"{sym}: {exc!s}")

    return summary
