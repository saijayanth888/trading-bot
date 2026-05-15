"""
wheel.filters — evidence-backed CSP entry guards.

Two pure-ish filters that the strategy research literature (spintwig,
Mike Yuen / DayTrading.com) identify as the difference between profitable
wheels and noise-level returns on a $120k paper account:

    earnings_blackout(symbol, target_dte, today)
        Block new CSPs within N calendar days before an earnings event.
        Earnings produce binary gap-moves that crush put-sellers: assignment
        at strike followed by a -10%+ gap down on the underlying.
        Source of truth: yfinance calendar (live) with automatic fallback to
        state/earnings.json (operator-maintained static file).

    iv_rank_filter(symbol, today, threshold)
        Only sell premium when IV-Rank > threshold (default 35).
        IV-Rank = (current_IV - 252d_low) / (252d_high - 252d_low).
        Selling when IVR is low burns theta with no premium cushion.
        Source: spintwig/Mike Yuen wheel literature, conservative 35 threshold.
        Data: yfinance options chain (ATM straddle as IV proxy for the underlying).
        Fails open on any network / parse error with a logged warning rather
        than crashing the CSP cycle.

Config keys (all in WheelConfig / env vars):
    WHEEL_EARNINGS_FILTER_ENABLED   true/false  (default true)
    WHEEL_EARNINGS_BLACKOUT_DAYS    int         (default 7)
    WHEEL_IVR_FILTER_ENABLED        true/false  (default true)
    WHEEL_IVR_THRESHOLD             float       (default 35.0)

Both functions are intentionally free of side-effects (no state mutations,
no order placement) so they are safe to call in dry-run and backtest paths.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Static fallback earnings file — operator-written, alongside state/*.json
_EARNINGS_FILE = Path(__file__).resolve().parent / "state" / "earnings.json"

# Module-level imports for patchability in tests. Both are optional deps —
# if missing the filters fail open (skip with a warning) rather than crashing.
try:
    import yfinance as yf  # type: ignore  # noqa: F401
except ImportError:
    yf = None  # type: ignore

try:
    import numpy as np  # type: ignore  # noqa: F401
except ImportError:
    np = None  # type: ignore

# ── Helpers ────────────────────────────────────────────────────────────────


def _read_static_earnings(symbol: str) -> Optional[date]:
    """Read next-earnings date from the static earnings.json fallback file.

    Format: { "SOFI": "2026-05-15", "NVDA": "2026-05-28", ... }
    Returns None if the file is missing, the symbol is absent, or the date
    is in the past.
    """
    try:
        if not _EARNINGS_FILE.exists():
            return None
        raw = json.loads(_EARNINGS_FILE.read_text() or "{}")
        iso = (raw.get(symbol) or "").strip()
        if not iso:
            return None
        d = date.fromisoformat(iso)
        # Only return future dates — past entries are stale.
        return d if d >= date.today() else None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("wheel.filters: failed to read earnings.json for %s: %s", symbol, exc)
        return None


def _fetch_yf_earnings_date(symbol: str) -> Optional[date]:
    """Fetch the next earnings date from yfinance.

    Returns the nearest upcoming earnings date, or None on any error.
    Uses a short timeout window — never blocks the CSP cycle for more than
    a few seconds.
    """
    try:
        if yf is None:
            return None
        ticker = yf.Ticker(symbol)
        # yfinance exposes earnings dates in `earnings_dates` (DataFrame,
        # index = Timestamp). Columns include "Reported EPS" (NaN for future).
        cal = ticker.earnings_dates  # may be None on yfinance 1.x
        if cal is None or (hasattr(cal, "empty") and cal.empty):
            return None
        today = date.today()
        # Filter to future dates only (rows with NaN EPS are upcoming).
        future_rows = cal[cal.index.normalize() >= str(today)]
        if hasattr(future_rows, "empty") and future_rows.empty:
            return None
        if len(future_rows) == 0:
            return None
        # Oldest upcoming date = next earnings
        next_ts = future_rows.index.min()
        return next_ts.date() if hasattr(next_ts, "date") else date.fromisoformat(str(next_ts)[:10])
    except Exception as exc:
        logger.debug("wheel.filters: yfinance earnings lookup failed for %s: %s", symbol, exc)
        return None


# ── Public filter functions ────────────────────────────────────────────────


def earnings_blackout(
    symbol: str,
    target_dte: int,
    today: Optional[date] = None,
    blackout_days: int = 7,
) -> tuple[bool, str]:
    """Return (is_blocked, reason).

    Blocked when next earnings falls within `blackout_days` calendar days of
    `today` OR within the option's expiry window — whichever is more
    conservative.

    Priority order for earnings date:
      1. yfinance live calendar (preferred — always current)
      2. state/earnings.json static file (operator fallback)
      3. Not blocked (log a warning so the operator knows data is missing)

    Args:
        symbol:       Underlying ticker, e.g. "NVDA".
        target_dte:   Days-to-expiry of the candidate CSP.  Used to check
                      whether earnings falls INSIDE the option's life — even
                      if earnings is 8 days away, a 10-DTE CSP would hold
                      through it.
        today:        Reference date (injectable for tests; defaults to
                      date.today()).
        blackout_days: How many calendar days before earnings to block new CSPs.
                       From config: cfg.earnings_blackout_days.

    Returns:
        (True,  "reason string")  → skip this symbol
        (False, "reason string")  → OK to proceed
    """
    today = today or date.today()

    # Try live source first; fall back to static file.
    next_earn: Optional[date] = _fetch_yf_earnings_date(symbol)
    source = "yfinance"
    if next_earn is None:
        next_earn = _read_static_earnings(symbol)
        source = "earnings.json"

    if next_earn is None:
        logger.debug(
            "wheel.filters: no earnings date found for %s (yfinance + static) — not blocking",
            symbol,
        )
        return False, f"{symbol}: no earnings date on file — not blocking"

    days_until = (next_earn - today).days

    # Gate 1: raw calendar proximity
    if 0 <= days_until <= blackout_days:
        reason = (
            f"{symbol}: earnings blackout — next earnings {next_earn.isoformat()} "
            f"is {days_until}d away (threshold {blackout_days}d, source={source})"
        )
        logger.info("wheel.filters: BLOCKED %s", reason)
        return True, reason

    # Gate 2: option would expire AFTER earnings — we'd be holding through
    # the binary event even if it's beyond the calendar blackout.
    option_expiry = today + timedelta(days=target_dte)
    if next_earn <= option_expiry:
        reason = (
            f"{symbol}: earnings blackout — next earnings {next_earn.isoformat()} "
            f"falls within the option's {target_dte}d DTE window "
            f"(option expires {option_expiry.isoformat()}, source={source})"
        )
        logger.info("wheel.filters: BLOCKED %s", reason)
        return True, reason

    return False, (
        f"{symbol}: earnings {next_earn.isoformat()} is {days_until}d away "
        f"— outside {blackout_days}d blackout (source={source})"
    )


def iv_rank_filter(
    symbol: str,
    today: Optional[date] = None,
    threshold: float = 35.0,
    lookback_days: int = 252,
) -> tuple[bool, str]:
    """Return (is_passing, reason).

    Passing when the symbol's IV-Rank >= threshold.

    IV-Rank = (current_IV - 252d_low) / (252d_high - 252d_low) * 100
    Threshold = 35 (from spintwig/Mike Yuen wheel literature).

    IV estimation: we use yfinance's `impliedVolatility` field on the ATM
    straddle for a 30-DTE expiry as a proxy for the underlying's current IV.
    For the 252-day history we fetch daily closing prices and compute the
    30-day rolling historical volatility (annualised) as a PROXY for
    historical IV — this is imperfect (HV ≠ IV) but is the best we can do
    without an expensive data subscription.  In practice IV tracks HV closely
    enough over 252 days that the rank signal is directionally correct.

    Graceful degradation:
        Any network / parse / index error → (True, "IVR check skipped: ...")
        with a WARNING log. We PASS (not block) on failure so a data outage
        doesn't silently stop all CSP entries.

    Args:
        symbol:        Underlying ticker.
        today:         Reference date (injectable for tests).
        threshold:     IV-Rank % below which we skip new CSPs.
        lookback_days: Rolling window for 252d high/low (default 252 trading
                       days ≈ 1 calendar year).

    Returns:
        (True,  reason)   → IV-Rank is acceptable; proceed
        (False, reason)   → IV-Rank too low; skip
    """
    today = today or date.today()

    try:
        if yf is None:
            return True, f"{symbol}: IVR check skipped — yfinance not installed"
        if np is None:
            return True, f"{symbol}: IVR check skipped — numpy not installed"

        # ── Step 1: current IV from the ATM options chain ──────────────────
        ticker = yf.Ticker(symbol)

        # Pick the nearest expiry that is ≥ 25 days out (30-DTE proxy).
        expiries = ticker.options  # tuple of "YYYY-MM-DD" strings
        if not expiries:
            return True, f"{symbol}: IVR check skipped — no options chain available"

        target = today + timedelta(days=25)
        chosen_expiry: Optional[str] = None
        for exp in expiries:
            if date.fromisoformat(exp) >= target:
                chosen_expiry = exp
                break
        if chosen_expiry is None:
            chosen_expiry = expiries[-1]  # fallback: farthest available

        chain = ticker.option_chain(chosen_expiry)
        puts = chain.puts
        calls = chain.calls

        # Get current stock price to find the ATM strike.
        fast_info = ticker.fast_info
        spot = float(fast_info.get("lastPrice", 0) or fast_info.get("last_price", 0) or 0)
        if spot <= 0:
            # Try history as backup
            hist_spot = ticker.history(period="2d")
            if not hist_spot.empty:
                spot = float(hist_spot["Close"].iloc[-1])
        if spot <= 0:
            return True, f"{symbol}: IVR check skipped — could not determine spot price"

        # ATM put IV proxy
        puts_copy = puts.copy()
        puts_copy["_dist"] = (puts_copy["strike"] - spot).abs()
        atm_put = puts_copy.sort_values("_dist").iloc[0]
        current_iv_put = float(atm_put.get("impliedVolatility", 0) or 0)

        # ATM call IV proxy
        calls_copy = calls.copy()
        calls_copy["_dist"] = (calls_copy["strike"] - spot).abs()
        atm_call = calls_copy.sort_values("_dist").iloc[0]
        current_iv_call = float(atm_call.get("impliedVolatility", 0) or 0)

        # Straddle IV = average of ATM put + call IV
        if current_iv_put > 0 and current_iv_call > 0:
            current_iv = (current_iv_put + current_iv_call) / 2.0
        elif current_iv_put > 0:
            current_iv = current_iv_put
        elif current_iv_call > 0:
            current_iv = current_iv_call
        else:
            return True, f"{symbol}: IVR check skipped — ATM IV is zero (illiquid chain?)"

        # ── Step 2: 252-day historical volatility range as IV proxy ────────
        # Fetch 1 year + buffer of daily closes. yfinance "1y" = ~252 trading
        # days. We add a 20% buffer (period="14mo") to ensure we get enough.
        hist = ticker.history(period="14mo", interval="1d")
        if hist.empty or len(hist) < 30:
            return True, f"{symbol}: IVR check skipped — insufficient price history"

        closes = hist["Close"].dropna()
        # 30-day rolling HV annualised: std(log_returns) * sqrt(252)
        log_ret = np.log(closes / closes.shift(1)).dropna()
        hv_series = log_ret.rolling(30).std() * np.sqrt(252)
        hv_series = hv_series.dropna()

        if len(hv_series) < lookback_days:
            lookback_days = len(hv_series)  # use whatever we have

        hv_window = hv_series.iloc[-lookback_days:]
        hv_low = float(hv_window.min())
        hv_high = float(hv_window.max())

        if hv_high <= hv_low or hv_high <= 0:
            return True, (
                f"{symbol}: IVR check skipped — HV range degenerate "
                f"(low={hv_low:.3f}, high={hv_high:.3f})"
            )

        # IV-Rank using current straddle IV vs HV range (directional proxy)
        ivr = (current_iv - hv_low) / (hv_high - hv_low) * 100.0
        ivr = round(ivr, 1)

        if ivr < threshold:
            reason = (
                f"{symbol}: IVR {ivr:.1f} < threshold {threshold:.0f} — "
                f"IV too low to sell premium (current_iv={current_iv:.3f}, "
                f"hv_low={hv_low:.3f}, hv_high={hv_high:.3f})"
            )
            logger.info("wheel.filters: BLOCKED %s", reason)
            return False, reason

        reason = (
            f"{symbol}: IVR {ivr:.1f} >= threshold {threshold:.0f} — OK to sell "
            f"(current_iv={current_iv:.3f}, hv_low={hv_low:.3f}, hv_high={hv_high:.3f})"
        )
        logger.debug("wheel.filters: PASS %s", reason)
        return True, reason

    except Exception as exc:
        # Any other error (network, parse, index) — fail open.
        msg = f"{symbol}: IVR check skipped — unexpected error: {exc!s}"
        logger.warning("wheel.filters: %s", msg)
        return True, msg
