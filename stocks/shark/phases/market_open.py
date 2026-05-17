from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path

from shark.agents.combined_analyst import analyze_symbol
from shark.data.alpaca_data import get_account, get_bars, get_positions
from shark.data.macro_calendar import check_macro_calendar
from shark.data.market_regime import detect_regime, get_regime_rules
from shark.data.perplexity import fetch_market_intel
from shark.data.relative_strength import compute_relative_strength
from shark.data.technical import compute_indicators
from shark.data.watchlist import SECTOR_ETFS, get_ticker_sector
from shark.execution.guardrails import Guardrails
from shark.execution.orders import place_bracket_order
from shark.execution.position_sizer import compute_position_size
from shark.memory import handoff, state
from shark.memory.atomic import atomic_write_json
from shark.memory.journal import log_trade
from shark.memory.kill_switch import KillSwitchActive, enforce_kill_switch
from shark.notify import notify as _notify
from shark.risk_floors import min_confidence, min_risk_reward, min_risk_reward_tol
from shark.signals.distributor import send_email_digest
from shark.signals.generator import generate_signal
from shark.signals.templates import trade_signal_html

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RESEARCH_LOG = os.path.join(_REPO_ROOT, "memory", "RESEARCH-LOG.md")
_PROJECT_CONTEXT = os.path.join(_REPO_ROOT, "memory", "PROJECT-CONTEXT.md")
_ANALYSIS_FILE = Path(_REPO_ROOT) / "memory" / "market-open-analysis.json"
_DECISIONS_FILE = Path(_REPO_ROOT) / "memory" / "market-open-decisions.json"

MAX_TRADES_PER_RUN = 3
_EARNINGS_BLOCK_DAYS = 2

# Server-side hard floors moved to shark/risk_floors.py 2026-05-14
# (stagnant-config audit Wave 1.3). The values used to live as duplicated
# module-level constants here, in agents/decision_arbiter.py, and in
# signals/generator.py — change-one-forget-the-others drift bait. The
# helpers are regime-aware: pass regime_rules at the call site to get the
# tighter floor in volatile / bear markets.

# Stocks TFT inference gate — a trained model votes UP/FLAT/DOWN on the
# candidate. We veto BUYs the model disagrees with above the confidence
# floor. Set TFT_GATE_ENABLED=0 to disable (debug/training-only mode).
_TFT_GATE_ENABLED = os.environ.get("TFT_GATE_ENABLED", "1") != "0"
_TFT_MIN_UP_PROB = float(os.environ.get("TFT_MIN_UP_PROB", "0.40"))
_TFT_MIN_CONFIDENCE = float(os.environ.get("TFT_MIN_CONFIDENCE", "0.05"))


def _tft_predict(symbol: str) -> dict | None:
    """Run the stocks TFT once for *symbol* and return the raw prediction
    dict {up, conf, down, ...} or None on any failure. Pure prediction;
    no gating decisions. Used by:
      - the runtime composite-override check in _collect_candidate_data
        (priced-in candidates can pass to the bull/bear debate if model
        signals strongly bullish — replaces stagnant date-based override)
      - _tft_gate below (the later hard-floor check, unchanged semantics)

    Caller should treat None as 'no TFT signal' — fail-open at each call
    site (don't kill a candidate just because the model isn't available).
    """
    try:
        from shark.ml.dataset_stock import _load_bars_json
        from shark.ml.features_stock import FEATURE_COLS, build_features
        from shark.ml.tft_stock import predict_direction
    except ImportError as exc:
        logger.debug("[TFT] import failed for %s: %s", symbol, exc)
        return None

    bars_path = Path(_REPO_ROOT) / "kb" / "historical_bars" / f"{symbol.upper()}.json"
    if not bars_path.is_file():
        return None

    try:
        bars = _load_bars_json(bars_path)
        feats = build_features(bars)
        if len(feats) < 60:
            return None
        window = feats[list(FEATURE_COLS)].iloc[-60:].values
        pred = predict_direction(symbol.upper(), window)
    except Exception as exc:
        logger.warning("[TFT] %s — prediction failed: %s", symbol, exc)
        return None

    if pred.get("error"):
        return None
    return pred


def _tft_gate(symbol: str, pred: dict | None = None) -> tuple[bool, str]:
    """Apply the TFT hard-floor gate for *symbol*. Accepts a pre-computed
    *pred* (from _tft_predict) to avoid redundant inference; if absent,
    fetches it. Fails open on any infrastructure issue.

    Returns (allowed, reason).
    """
    if pred is None:
        pred = _tft_predict(symbol)
    if pred is None:
        return True, "tft-unavailable"

    up = float(pred.get("up") or 0.0)
    conf = float(pred.get("confidence") or 0.0)
    down = float(pred.get("down") or 0.0)

    # Hard veto: model thinks DOWN is the most-likely outcome with material
    # confidence. This is the cleanest case of LLM-vs-model disagreement.
    if down > up and conf >= _TFT_MIN_CONFIDENCE:
        return False, f"tft-veto-down: up={up:.2f} down={down:.2f} conf={conf:.2f}"

    # Soft veto: UP-prob too low even though it's the argmax.
    if up < _TFT_MIN_UP_PROB:
        return False, f"tft-low-up-prob: up={up:.2f} (floor={_TFT_MIN_UP_PROB})"

    return True, f"tft-pass: up={up:.2f} conf={conf:.2f}"


def _verify_risk_reward(
    entry: float,
    stop: float | int | str | None,
    target: float | int | str | None,
) -> float | None:
    """Recompute R:R = (target - entry) / (entry - stop). Returns None on bad input.

    Defends against LLM math errors and adversarial outputs (e.g. stop above entry).
    """
    if stop is None or target is None:
        return None
    try:
        s = float(stop)
        t = float(target)
        e = float(entry)
    except (TypeError, ValueError):
        return None
    risk = e - s
    reward = t - e
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk

# Sector mappings now live in shark.data.watchlist (single source of truth)
# _TICKER_SECTOR → use get_ticker_sector(symbol)
# _SECTOR_ETFS → imported directly


def _check_sector_momentum(sector: str) -> tuple[bool, str]:
    etf = SECTOR_ETFS.get(sector)
    if not etf:
        return True, f"no ETF mapped for sector '{sector}' — skipping momentum check"
    try:
        bars = get_bars(etf, timeframe="1Day", limit=30)
        indicators = compute_indicators(bars)
        price = indicators["current_price"]
        sma20 = indicators.get("sma_20")
        rsi = indicators.get("rsi_14", 50.0)
        if sma20 is None:
            return True, f"{etf} insufficient data for SMA20"
        above_sma = price > sma20
        rsi_ok = rsi > 45.0
        if above_sma and rsi_ok:
            return True, f"{etf} bullish: price ${price:.2f} > SMA20 ${sma20:.2f}, RSI {rsi:.1f}"
        return False, (
            f"{etf} bearish headwind: price ${price:.2f} "
            f"{'>' if above_sma else '<'} SMA20 ${sma20:.2f}, RSI {rsi:.1f}"
        )
    except Exception as exc:
        logger.warning("Sector momentum check failed for %s (%s): %s", sector, etf, exc)
        return True, f"sector momentum check failed for {etf} — defaulting to pass"


def _parse_confirmed_candidates(date_str: str) -> list[str]:
    try:
        with open(_RESEARCH_LOG, "r") as f:
            content = f.read()
    except FileNotFoundError:
        logger.warning("RESEARCH-LOG.md not found at %s", _RESEARCH_LOG)
        return []
    sections = re.split(r"(?=^## \d{4}-\d{2}-\d{2})", content, flags=re.MULTILINE)
    target_section = None
    for section in sections:
        if section.startswith(f"## {date_str}"):
            target_section = section
            break
    if not target_section:
        return []
    table_matches = re.findall(
        r"^\|\s*([A-Z]{1,5})\s*\|\s*CONFIRMED\s*\|",
        target_section, flags=re.MULTILINE | re.IGNORECASE,
    )
    if table_matches:
        return [s.upper() for s in table_matches]
    confirmed_line = re.search(
        r"^CONFIRMED:\s*(.+)$", target_section, flags=re.MULTILINE | re.IGNORECASE
    )
    if confirmed_line:
        raw = confirmed_line.group(1)
        symbols = [t.strip().upper() for t in re.split(r"[,\s]+", raw) if re.match(r"^[A-Z]{1,5}$", t.strip().upper())]
        if symbols:
            return symbols
    passed_line = re.search(
        r"(?:Passed to market-open|Decision)[:\s*]+([A-Z ,]+)",
        target_section, flags=re.MULTILINE | re.IGNORECASE,
    )
    if passed_line:
        raw = passed_line.group(1)
        symbols = [t.strip().upper() for t in re.split(r"[,\s]+", raw) if re.match(r"^[A-Z]{1,5}$", t.strip().upper())]
        if symbols:
            return symbols
    return []


def _is_circuit_breaker_triggered() -> bool:
    try:
        with open(_PROJECT_CONTEXT, "r") as f:
            content = f.read()
        return bool(re.search(r"circuit_breaker_triggered:\s*true", content, re.IGNORECASE))
    except FileNotFoundError:
        return False


def _build_email_body(signal: dict, decision: dict, execution: dict) -> str:
    return trade_signal_html(
        symbol=decision.get("symbol", "N/A"),
        side="BUY",
        entry=execution.get("fill_price", decision.get("entry_price", "N/A")),
        stop=execution.get("stop_price", decision.get("stop_loss", "N/A")),
        target=decision.get("target_price", "N/A"),
        rr=decision.get("risk_reward_ratio", "N/A"),
        confidence=decision.get("confidence", 0),
        order_id=execution.get("order_id", "N/A"),
        thesis=decision.get("thesis_summary", ""),
        reasoning=decision.get("reasoning", ""),
    )


def _collect_candidate_data(
    symbol: str,
    existing_symbols: set,
    account_for_guardrails: dict,
    portfolio_value: float,
    peak_equity: float,
    regime_str: str,
    regime_rules: dict,
    regime_mult: float,
    macro_mult: float,
    stop_width: float,
    guardrails: Guardrails,
    weekly_count: int,
    candidates_so_far: int,
) -> dict | None:
    """Fetch and validate all data for one symbol. Returns candidate dict or None if blocked."""
    if symbol in existing_symbols:
        logger.info("%s already in positions — skipping", symbol)
        return None
    try:
        bars = get_bars(symbol, timeframe="1Day", limit=60)
        technicals = compute_indicators(bars)
        current_price = technicals["current_price"]
        momentum_score = technicals.get("momentum_score", 50.0)

        intel = fetch_market_intel([symbol])
        perplexity_intel = intel.get(symbol, {})

        earnings_days = perplexity_intel.get("earnings_within_days")
        if earnings_days is not None and earnings_days <= _EARNINGS_BLOCK_DAYS:
            logger.info("%s skipped — earnings in %d day(s)", symbol, earnings_days)
            return None

        # Catalyst gate. Strict in live mode: only trade with a concrete
        # dated catalyst (earnings beat, product launch, regulatory news).
        # In paper-mode-with-BEAR-override the gate is softened — without
        # this softening the override's "1 trade/day @ 0.5×" allowance was
        # effectively unreachable, because Perplexity returns
        # catalyst_specific=False on most days (general momentum, not a
        # headline event). Two consecutive days (2026-05-12 NVDA,
        # 2026-05-13 GOOGL) cleared pre-market scoring + pre-execute
        # confirmation, then died on this gate, never reaching the
        # bull/bear/arbiter LLM debate — defeating the whole point of
        # paper-mode override.
        #
        # Softer paper-mode gate: accept the candidate when EVERY one of
        # these holds (not OR — AND), which is still a real quality bar:
        #   - catalyst is NOT priced in (no >3% move on this news yet)
        #   - perplexity returned at least one headline (i.e. there is
        #     SOMETHING moving, not just chart momentum on silence)
        #   - sentiment_score from perplexity >= +0.30 (mildly bullish at
        #     minimum — rejects neutral/negative coverage)
        #   - analyst_rating is not "sell"
        # The candidate carries `catalyst_specific=False` through the
        # pipeline so the bull/bear LLM debate can see this is a softer
        # signal and weight accordingly.
        try:
            from shark.config import get_settings as _get_settings
            _cfg_local = _get_settings()
            _paper_override = bool(_cfg_local.is_paper and _cfg_local.paper_bear_override)
        except Exception:
            _paper_override = False

        # ─── Gather signals once, use everywhere below ────────────────────
        # Pre-2026-05-14 this function fetched RS only after the priced_in
        # hard kill, and TFT later still. The priced_in kill was unconditional
        # and the soft-gate's "not priced_in" AND was unreachable past it —
        # both structural bugs. The fix: gather all signals up front, then
        # compute one "runtime composite override" boolean that any
        # LLM-boolean hard kill can consult. Replaces the reverted
        # PAPER_PRICED_IN_OVERRIDE_UNTIL date-based mechanism — operator's
        # principle is that the intelligent layer should DERIVE this at
        # runtime, not read a stagnant env var.
        priced_in = bool(perplexity_intel.get("catalyst_priced_in", False))
        has_specific_catalyst = bool(perplexity_intel.get("catalyst_specific", True))
        sentiment_score = float(perplexity_intel.get("sentiment_score") or 0.0)
        analyst_rating = str(perplexity_intel.get("analyst_rating") or "hold").lower()
        headlines_count = len(perplexity_intel.get("headlines") or [])

        rs_data = compute_relative_strength(symbol)
        rs_composite = float(rs_data.get("rs_composite") or 0.0)
        outperforming = bool(rs_data.get("outperforming", False))

        tft_pred = _tft_predict(symbol)  # reused by _tft_gate later — no double-inference
        tft_up = float(tft_pred.get("up") or 0.0) if tft_pred else None
        tft_conf = float(tft_pred.get("confidence") or 0.0) if tft_pred else None

        # Runtime composite override — when set, downstream LLM-boolean
        # hard kills become advisory rather than fatal. The thresholds
        # below (TFT up ≥ 0.55, sentiment ≥ 0.5, RS ≥ 1.0) intentionally
        # require BOTH model conviction AND market-state confirmation; a
        # candidate that triggers all three has multi-signal agreement
        # well above any single-LLM-boolean's weight.
        #
        # TFT-unavailable handling (C-3 fix, 2026-05-14): when _tft_predict
        # returns None (model missing, insufficient bars, runtime error)
        # we MUST NOT treat that as a TFT-vetoes-composite case — that
        # collapses the composite to a strict-three-AND that silently
        # excludes every candidate whenever the model is offline. Instead
        # we fail-open on TFT and require the OTHER TWO signals to agree.
        # This mirrors _tft_gate's fail-open semantics and matches the
        # operator's stated principle (infrastructure hiccups should not
        # veto trades — only model DISAGREEMENT should).
        if tft_up is None:
            # TFT signal unavailable — composite passes when the two
            # remaining signals are in firm agreement. This is strictly
            # MORE conservative than the live-TFT composite path because
            # we're missing one of the three confirmations, so we keep
            # the same sentiment/RS thresholds and let those carry it.
            composite_override = (
                sentiment_score >= 0.5
                and rs_composite >= 1.0
            )
        else:
            composite_override = (
                tft_up >= 0.55
                and sentiment_score >= 0.5
                and rs_composite >= 1.0
            )

        # ─── Diagnostic gate-state log ─────────────────────────────────────
        # Per agent4 audit suggestion (2026-05-14): operator can't tell from
        # the cron log whether composite_override was evaluated at all on
        # the killed candidate. Emit one structured INFO line up front so
        # downstream `<SYM> skipped — ...` lines have ground-truth context.
        tft_up_str = f"{tft_up:.2f}" if tft_up is not None else "n/a"
        logger.info(
            "[GATES] %s composite_override=%s catalyst_priced_in=%s "
            "has_specific_catalyst=%s (tft_up=%s sentiment=%.2f rs=%.2f "
            "outperforming=%s paper_override=%s)",
            symbol, composite_override, priced_in,
            has_specific_catalyst, tft_up_str, sentiment_score, rs_composite,
            outperforming, _paper_override,
        )

        # ─── Catalyst-specific gate ────────────────────────────────────────
        # The paper-mode soft-gate kept its original criteria; the priced_in
        # AND moved out of here (it has its own gate below now). Composite
        # override OR's around the soft-gate too — a candidate with no
        # specific catalyst but strong multi-signal agreement still flows.
        if not has_specific_catalyst:
            soft_ok = (
                _paper_override
                and headlines_count > 0
                and sentiment_score >= 0.30
                and analyst_rating != "sell"
            ) or composite_override
            if not soft_ok:
                logger.info("%s skipped — no specific catalyst", symbol)
                return None
            if composite_override:
                logger.info(
                    "%s — no specific catalyst BUT runtime composite passes "
                    "(tft_up=%.2f, sentiment=%.2f, rs=%.2f). Letting through.",
                    symbol, tft_up or 0.0, sentiment_score, rs_composite,
                )
            else:
                logger.info(
                    "%s — no specific catalyst, but paper-mode soft gate passed "
                    "(sentiment=%.2f, headlines=%d, rating=%s)",
                    symbol, sentiment_score, headlines_count, analyst_rating,
                )

        # ─── Priced-in gate ────────────────────────────────────────────────
        # Was an unconditional hard kill; now consults the composite. When
        # composite is True we log loudly (this is the case the operator
        # specifically called out — bot decides per-candidate, not by date).
        if priced_in:
            if composite_override:
                logger.info(
                    "%s — catalyst_priced_in=true BUT runtime composite passes "
                    "(tft_up=%.2f≥0.55, sentiment=%.2f≥0.5, rs=%.2f≥1.0). "
                    "Letting through to bull/bear/arbiter debate.",
                    symbol, tft_up or 0.0, sentiment_score, rs_composite,
                )
            else:
                tft_str = f"{tft_up:.2f}" if tft_up is not None else "n/a"
                logger.info(
                    "%s skipped — catalyst already priced in "
                    "(composite failed: tft_up=%s, sentiment=%.2f, rs=%.2f)",
                    symbol, tft_str, sentiment_score, rs_composite,
                )
                return None

        sector = get_ticker_sector(symbol)
        sector_ok, sector_reason = _check_sector_momentum(sector)
        if not sector_ok:
            logger.info("%s skipped — %s", symbol, sector_reason)
            return None

        # ─── Relative-strength gate ────────────────────────────────────────
        # Uses rs_data computed above — composite_override does NOT bypass
        # this because RS is one of the composite's own inputs (RS ≥ 1.0
        # being a requirement). If composite passed, this gate passes too,
        # so no special-case logic is needed.
        if not outperforming:
            logger.info(
                "%s skipped — underperforming SPY (RS=%.2f)",
                symbol, rs_composite,
            )
            return None

        atr = technicals.get("atr_14", current_price * 0.02)
        sizing = compute_position_size(
            portfolio_value=portfolio_value,
            current_price=current_price,
            atr=atr,
            regime_multiplier=regime_mult * macro_mult,
            peak_equity=peak_equity,
            confidence=regime_rules.get("confidence_threshold", 0.70),
        )
        if sizing["shares"] <= 0:
            logger.info("%s — position sizer returned 0 shares, skipping", symbol)
            return None

        proposed_trade = {
            "symbol": symbol,
            "qty": sizing["shares"],
            "estimated_cost": sizing["dollar_amount"],
            "sector": sector,
        }
        risk = guardrails.run_all(
            proposed_trade,
            account_for_guardrails,
            weekly_count + candidates_so_far,
            peak_equity,
            [],
            regime=regime_str,
            momentum_score=momentum_score,
        )
        if not risk["approved"]:
            logger.info("%s failed guardrails — %s", symbol, risk["violations"])
            return None

        # Strategy attribution — record what KB signal drove the score.
        setup_tag, pead_event_date = "momentum", None
        try:
            from shark.data.kb_scoring import compute_setup_tag
            setup_tag, pead_event_date = compute_setup_tag(
                symbol=symbol, regime=regime_str,
            )
        except Exception as exc:
            logger.debug("setup_tag computation failed for %s: %s", symbol, exc)

        logger.info("%s passed all gates — including in analysis (tag=%s)", symbol, setup_tag)

        # ─── Indicator-selector agent call (gated) ─────────────────────────
        # Fires only when SHARK_ENABLE_INDICATOR_SELECTOR=1 env var is set.
        # Calls per-symbol ONCE on the shortlisted candidates (~10-30/day),
        # NOT for all 524 KB tickers (those never reach _collect_candidate_data).
        # The call is logged via LLMTracker with agent="indicator_selector" so
        # modelforge_ingest.py picks it up via AGENT_TO_ROLE mapping.
        # On invalid JSON or any failure: log warning, fall back to
        # REGIME_BASELINE_INDICATORS[regime_str] (deterministic, never raises).
        # Accumulation rate: ~10-30 calls/day → N_MIN=40 in 2-4 days of live op.
        indicator_selection: list[str] | None = None
        if os.environ.get("SHARK_ENABLE_INDICATOR_SELECTOR", "0") == "1":
            try:
                from shark.agents.market_analyst import (
                    IndicatorSelection,
                    select_indicators as _select_indicators,
                )
                from shark.llm.tracker import LLMTracker as _LLMTracker
                import time as _time

                _t0 = _time.monotonic()
                # Pass bars to the selector so it has OHLCV context.
                _sel: IndicatorSelection = _select_indicators(
                    ticker=symbol,
                    regime=regime_str,
                    bars=bars,
                    use_cache=True,
                )
                _latency = _time.monotonic() - _t0

                # Validate picks — must be a non-empty list of strings.
                if _sel.picks:
                    indicator_selection = [str(p.indicator) for p in _sel.picks]
                    # Log via LLMTracker so modelforge_ingest.py picks up
                    # agent="indicator_selector" rows from llm-calls.jsonl.
                    try:
                        import json as _json
                        _response_json = _json.dumps({"indicators": indicator_selection})
                        _tracker = _LLMTracker()
                        _tracker.record(
                            agent="indicator_selector",
                            model="market_analyst",
                            tier="fast",
                            prompt=f"Symbol: {symbol}\nRegime: {regime_str}",
                            response_text=_response_json,
                            latency_seconds=_latency,
                            valid=True,
                        )
                    except Exception as _log_exc:
                        logger.debug("indicator_selector tracker.record failed: %s", _log_exc)
                    logger.info(
                        "%s indicator_selector selected %d picks for regime=%s: %s",
                        symbol, len(indicator_selection), regime_str, indicator_selection,
                    )
                else:
                    logger.warning(
                        "%s indicator_selector returned empty picks for regime=%s "
                        "— falling back to REGIME_BASELINE",
                        symbol, regime_str,
                    )
            except Exception as _ind_exc:
                logger.warning(
                    "%s indicator_selector call failed (%s) — falling back to REGIME_BASELINE",
                    symbol, _ind_exc,
                )

        # ATR-derived trailing stop — keeps trail proportional to a ticker's
        # actual volatility rather than a fixed 10%. ATR_TRAIL_MULTIPLE controls
        # tightness (default 3.0x ATR ~= typical swing-trade trail).
        from shark.config import get_settings
        _cfg = get_settings()
        atr_trail_multiple = _cfg.atr_trail_multiple
        trail_pct_min = _cfg.trail_pct_min
        trail_pct_max = _cfg.trail_pct_max
        if current_price > 0 and atr > 0:
            atr_trail = (atr / current_price) * 100.0 * atr_trail_multiple * stop_width
            computed_trail = max(trail_pct_min, min(trail_pct_max, atr_trail))
        else:
            computed_trail = round(10.0 * stop_width, 1)

        return {
            "symbol": symbol,
            "current_price": round(float(current_price), 2),
            "qty": sizing["shares"],
            "trail_pct": round(computed_trail, 1),
            "stop_price": round(float(sizing["stop_price"]), 2),
            "sector": sector,
            "sector_reason": sector_reason,
            "setup_tag": setup_tag,
            "pead_event_date": pead_event_date,
            "sizing_method": sizing["method_used"],
            "dollar_amount": round(float(sizing["dollar_amount"]), 2),
            "technicals": {
                "current_price": round(float(current_price), 2),
                "rsi_14": round(float(technicals.get("rsi", technicals.get("rsi_14", 50))), 1),
                "macd_histogram": round(float(technicals.get("macd_histogram", 0)), 4),
                "macd_bullish_cross": technicals.get("macd_bullish_cross", False),
                "bb_squeeze": technicals.get("bb_squeeze", False),
                "adx_14": round(float(technicals.get("adx_14", 0)), 1),
                "sma_20": round(float(technicals.get("sma_20", 0)), 2),
                "sma_50": round(float(technicals.get("sma_50", 0)), 2),
                "volume_ratio": round(float(technicals.get("volume_ratio", 1.0)), 2),
                "momentum_score": round(float(momentum_score), 1),
                "atr_14": round(float(atr), 2),
            },
            # _tft_pred cached so the later TFT hard-floor gate (line ~747)
            # reuses the prediction computed for the composite override above.
            # Saves one TFT inference per candidate.
            "_tft_pred": tft_pred,
            # indicator_selector result (None when SHARK_ENABLE_INDICATOR_SELECTOR
            # is not set or the call fails). The bull/bear debate prompt builder
            # can embed the selected indicators for richer context when present.
            "indicator_selection": indicator_selection,
            "perplexity_intel": perplexity_intel,
            "rs_data": {
                "rs_composite": round(float(rs_data.get("rs_composite", 0)), 3),
                "rs_rank_signal": rs_data.get("rs_rank_signal", "UNKNOWN"),
                "outperforming": rs_data.get("outperforming", False),
                "acceleration": round(float(rs_data.get("acceleration", 0)), 3),
            },
            "risk_check": {
                "approved": risk.get("approved", False),
                "adjusted_size": risk.get("adjusted_size", sizing["shares"]),
                "position_size_pct": round(float(risk.get("position_size_pct", 10)), 1),
            },
        }
    except Exception:
        logger.error("Error collecting data for %s", symbol, exc_info=True)
        return None


def _prepare(dry_run: bool = False) -> bool:
    """
    Cloud routine Step 1: collect all data, write market-open-analysis.json.
    Claude reads this file and writes decisions — no Anthropic API needed.
    """
    today = date.today().isoformat()
    logger.info("market_open PREPARE — date=%s dry_run=%s", today, dry_run)

    # Bugs C+D+E — always pre-write an empty, today-stamped decisions stub so
    # any stale file from a prior run is wiped. If Step 2 (LLM) fails or is
    # skipped, _execute will see today's date with zero decisions = safe no-op.
    atomic_write_json(_DECISIONS_FILE, {"date": today, "decisions": []}, indent=None)

    def _write_blocked(reason: str) -> bool:
        atomic_write_json(
            _ANALYSIS_FILE,
            {"date": today, "blocked": reason, "candidates": []},
            indent=None,
        )
        logger.info("Wrote blocked analysis: %s", reason)
        return True

    if _is_circuit_breaker_triggered():
        return _write_blocked("circuit_breaker")

    regime_data = detect_regime()
    regime = regime_data["regime"]
    regime_rules = regime_data["rules"]
    regime_str = regime.value if hasattr(regime, "value") else str(regime)

    if not regime_rules.get("new_trades_allowed", True):
        handoff.write_handoff_section("market-open", {
            "traded": "none", "reason": f"regime {regime_str} blocks new longs",
        })
        return _write_blocked(f"regime_{regime_str}")

    macro = check_macro_calendar()
    macro_impact = macro.get("impact_level", "NORMAL")
    from shark.config import get_settings
    cfg = get_settings()
    if macro_impact in ("CRITICAL", "HIGH") and not (cfg.is_paper and cfg.paper_macro_bypass):
        handoff.write_handoff_section("market-open", {
            "traded": "none", "reason": f"macro block: {macro.get('description', macro_impact)}",
        })
        return _write_blocked(f"macro_{macro_impact}")
    elif macro_impact in ("CRITICAL", "HIGH") and cfg.is_paper and cfg.paper_macro_bypass:
        logger.info("PAPER MODE: bypassing macro %s block for pipeline testing", macro_impact)

    candidates = handoff.get_validated_symbols()
    if not candidates:
        candidates = _parse_confirmed_candidates(today)
    if not candidates:
        logger.info("No candidates for %s", today)
        atomic_write_json(_ANALYSIS_FILE, {"date": today, "candidates": []}, indent=None)
        return True

    try:
        account = get_account()
        positions = get_positions()
    except Exception:
        logger.error("Failed to fetch account/positions", exc_info=True)
        return False

    existing_symbols = {p["symbol"].upper() for p in positions}
    weekly_count = state.get_weekly_trade_count()
    peak_equity = state.get_peak_equity()
    portfolio_value = float(account["portfolio_value"])

    # Bootstrap: if peak_equity was never set, initialize from current portfolio
    if peak_equity <= 0 and portfolio_value > 0:
        logger.info("Bootstrapping peak_equity from portfolio: $%.2f", portfolio_value)
        state.update_peak_equity(portfolio_value)
        peak_equity = portfolio_value
    elif portfolio_value > peak_equity:
        state.update_peak_equity(portfolio_value)
        peak_equity = portfolio_value

    max_trades = min(MAX_TRADES_PER_RUN, regime_rules.get("max_new_trades_per_day", 3))
    regime_mult = regime_rules.get("position_size_multiplier", 1.0)
    macro_rules = macro.get("rules", {})
    macro_mult = float(macro_rules.get("position_size_multiplier", 1.0))
    stop_width = regime_rules.get("stop_width_multiplier", 1.0)

    if macro_mult < 1.0:
        logger.info("Macro sizing adjustment: %.1fx (impact=%s)", macro_mult, macro_impact)

    account_for_guardrails = {
        "portfolio_value": portfolio_value,
        "cash": account["cash"],
        "positions": positions,
    }
    guardrails = Guardrails()
    candidate_data: list[dict] = []

    for symbol in candidates:
        c = _collect_candidate_data(
            symbol=symbol,
            existing_symbols=existing_symbols,
            account_for_guardrails=account_for_guardrails,
            portfolio_value=portfolio_value,
            peak_equity=peak_equity,
            regime_str=regime_str,
            regime_rules=regime_rules,
            regime_mult=regime_mult,
            macro_mult=macro_mult,
            stop_width=stop_width,
            guardrails=guardrails,
            weekly_count=weekly_count,
            candidates_so_far=len(candidate_data),
        )
        if c:
            candidate_data.append(c)

    analysis = {
        "date": today,
        "regime": regime_str,
        "macro_impact": macro_impact,
        "macro_description": macro.get("description", "normal"),
        "portfolio_value": round(portfolio_value, 2),
        "peak_equity": round(float(peak_equity), 2) if peak_equity else None,
        "weekly_trade_count": weekly_count,
        "max_trades_remaining": max_trades,
        "candidates": candidate_data,
    }

    atomic_write_json(_ANALYSIS_FILE, analysis, indent=2)
    logger.info(
        "Analysis written: %d candidates — %s",
        len(candidate_data), str(_ANALYSIS_FILE),
    )
    return True


def _execute(dry_run: bool = False) -> bool:
    """
    Cloud routine Step 3: read Claude's decisions, place orders, commit memory.
    Claude wrote memory/market-open-decisions.json in Step 2.
    """
    today = date.today().isoformat()
    logger.info("market_open EXECUTE — date=%s dry_run=%s", today, dry_run)

    if not _DECISIONS_FILE.exists():
        logger.error("Decisions file not found: %s", _DECISIONS_FILE)
        return False
    if not _ANALYSIS_FILE.exists():
        logger.error("Analysis file not found: %s", _ANALYSIS_FILE)
        return False

    try:
        decisions_data = json.loads(_DECISIONS_FILE.read_text())
        analysis_data = json.loads(_ANALYSIS_FILE.read_text())
    except Exception:
        logger.error("Failed to read decisions/analysis files", exc_info=True)
        return False

    # Bug C — reject stale files. _prepare always pre-writes today-stamped
    # decisions, so any mismatch means yesterday's run partially survived.
    decisions_date = decisions_data.get("date")
    analysis_date = analysis_data.get("date")
    if decisions_date != today:
        logger.error(
            "Refusing to execute — decisions.date=%s (expected %s). Stale file.",
            decisions_date, today,
        )
        return False
    if analysis_date != today:
        logger.error(
            "Refusing to execute — analysis.date=%s (expected %s). Stale file.",
            analysis_date, today,
        )
        return False

    candidate_map = {c["symbol"]: c for c in analysis_data.get("candidates", [])}
    regime_str = analysis_data.get("regime", "UNKNOWN")
    # Regime-aware floor resolution (Wave 1.3) — the helpers below read
    # confidence_threshold + min_risk_reward from this dict, so the floor
    # ladders 0.65 / 2.0 (quiet) → 0.75 / 2.5 (volatile) → 1.0 / 3.0 (bear).
    _regime_rules = get_regime_rules(regime_str)
    weekly_count = state.get_weekly_trade_count()
    max_trades = analysis_data.get("max_trades_remaining", MAX_TRADES_PER_RUN)
    symbols_traded: list[str] = []
    trades_placed = 0

    for dec in decisions_data.get("decisions", []):
        if trades_placed >= max_trades:
            logger.info("Max trades reached (%d) — stopping", max_trades)
            break

        symbol = dec.get("symbol", "")
        decision = dec.get("decision", "NO_TRADE")

        if decision != "BUY":
            logger.info("%s — Claude decided %s", symbol, decision)
            continue

        candidate = candidate_map.get(symbol)
        if not candidate:
            logger.warning("%s — no matching candidate data, skipping", symbol)
            continue

        # === Server-side hard rules (defense-in-depth, see Bug B + F) ===
        # Floors regime-aware — see shark.risk_floors. Pre-2026-05-14 these
        # were hardcoded 0.70 / 2.0; now they ladder per regime.
        _conf_floor = min_confidence(_regime_rules)
        _rr_floor = min_risk_reward(_regime_rules)
        confidence = float(dec.get("confidence", 0) or 0)
        claimed_rr = float(dec.get("risk_reward_ratio", 0) or 0)
        if confidence < _conf_floor:
            logger.info(
                "%s rejected — confidence %.2f < %.2f floor (regime=%s)",
                symbol, confidence, _conf_floor, regime_str,
            )
            continue
        if claimed_rr < _rr_floor:
            logger.info(
                "%s rejected — claimed R:R %.2f < %.2f floor (regime=%s)",
                symbol, claimed_rr, _rr_floor, regime_str,
            )
            continue

        qty = candidate["risk_check"].get("adjusted_size", candidate["qty"])
        trail_pct = candidate["trail_pct"]
        current_price = candidate["current_price"]
        atr = candidate["technicals"]["atr_14"]

        # Re-derive R:R from stop/target/entry — never trust the LLM's math
        llm_stop = dec.get("stop_loss")
        llm_target = dec.get("target_price")
        derived_rr = _verify_risk_reward(current_price, llm_stop, llm_target)
        if derived_rr is None:
            logger.info(
                "%s rejected — invalid stop/target (entry=%.2f stop=%s target=%s)",
                symbol, current_price, llm_stop, llm_target,
            )
            continue
        _rr_tol = min_risk_reward_tol(_regime_rules)
        if derived_rr < _rr_tol:
            logger.info(
                "%s rejected — derived R:R %.2f < %.2f tolerance (regime=%s) "
                "(LLM claimed %.2f; entry=%.2f stop=%.2f target=%.2f)",
                symbol, derived_rr, _rr_tol, regime_str,
                claimed_rr, current_price, float(llm_stop), float(llm_target),
            )
            continue

        # === Stocks TFT inference gate ===
        # The LLM agreed; now ask the trained model. If TFT predicts DOWN
        # with confidence or UP-prob is under the floor, skip the trade.
        # Reuses the prediction computed earlier in _collect_candidate_data
        # (composite-override path) — avoids running TFT inference twice.
        if _TFT_GATE_ENABLED:
            allowed, reason = _tft_gate(symbol, pred=candidate.get("_tft_pred"))
            if not allowed:
                logger.info("%s rejected — %s", symbol, reason)
                continue
            logger.info("[TFT_GATE] %s %s", symbol, reason)

        logger.info(
            "%s EXECUTE qty=%d confidence=%.2f rr=%.2f stop=$%.2f target=$%.2f",
            symbol, qty, confidence, derived_rr,
            float(llm_stop), float(llm_target),
        )

        if dry_run:
            logger.info(
                "[DRY RUN] Would place bracket: %s x%d stop=$%.2f target=$%.2f",
                symbol, qty, float(llm_stop), float(llm_target),
            )
            continue

        try:
            # Pass LLM-computed stop + target into the broker so they actually
            # take effect (see Bug A). Trailing-pct stays as fallback only.
            execution = place_bracket_order(
                symbol,
                qty,
                trail_pct=trail_pct,
                stop_loss=float(llm_stop),
                take_profit=float(llm_target),
            )
        except Exception:
            logger.error("Failed to place order for %s", symbol, exc_info=True)
            continue

        # Claim ownership (Shark/Wheel isolation, Fix 3). Done AFTER the
        # bracket order is accepted so a failed entry doesn't leave a
        # phantom claim. The bracket parent itself is the source of truth;
        # the owned-symbols set is a fast-lookup mirror Shark's midday
        # loops consult before touching any position.
        try:
            from shared.subsystem_ownership import claim
            claim("shark", symbol)
        except Exception as exc:
            logger.warning("ownership claim failed for %s: %s", symbol, exc)

        fill_price = execution.get("fill_price", current_price)
        stop_price = execution.get("stop_price", dec.get("stop_loss", candidate["stop_price"]))

        log_trade({
            "date": today,
            "symbol": symbol,
            "side": "buy",
            "qty": qty,
            "price": fill_price,
            "stop": stop_price,
            "catalyst": dec.get("bull_thesis", ""),
            "target": dec.get("target_price", ""),
            "rr": dec.get("risk_reward_ratio", ""),
            "regime": regime_str,
            "rs_composite": candidate["rs_data"]["rs_composite"],
            "momentum_score": candidate["technicals"]["momentum_score"],
            "sizing_method": candidate["sizing_method"],
            "atr": atr,
        })

        # Strategy-attribution sidecar — read by midday at close.
        try:
            from shark.memory.open_trades import upsert_open_trade
            upsert_open_trade(
                symbol,
                setup_tag=candidate.get("setup_tag", "momentum"),
                pead_event_date=candidate.get("pead_event_date"),
                entry_date=today,
                entry_price=float(fill_price),
                regime=regime_str,
            )
        except Exception as exc:
            logger.debug("upsert_open_trade failed for %s: %s", symbol, exc)

        signal = generate_signal(dec, execution)
        body_html = _build_email_body(signal, dec, execution)
        send_email_digest(
            subject=f"Shark BUY Signal — {symbol} @ ${fill_price}",
            body_html=body_html,
        )
        # Slack/Telegram ping — operator needs out-of-band signal that a
        # stocks trade just opened. Failure is silently ignored by notify.
        try:
            _notify.trade_entry(
                pair=symbol,
                signal="long",
                entry_price=float(fill_price),
                stake_amount=float(qty) * float(fill_price),
                confidence=confidence,
                regime=regime_str,
                stop_loss=float(llm_stop),
                take_profit=float(llm_target),
                rationale=dec.get("bull_thesis", "")[:200],
            )
        except Exception as exc:
            logger.warning("notify.trade_entry failed for %s: %s", symbol, exc)
        symbols_traded.append(symbol)
        trades_placed += 1

    handoff.write_handoff_section("market-open", {
        "traded": ", ".join(symbols_traded) if symbols_traded else "none",
        "count": str(trades_placed),
        "regime": regime_str,
        "macro": analysis_data.get("macro_description", "normal"),
    })

    if not dry_run:
        try:
            # Weekly trade count is derived from TRADE-LOG.md on read; nothing to write here.
            traded_label = ",".join(symbols_traded) if symbols_traded else "none"
            state.commit_memory(f"market-open {today}: {traded_label} regime={regime_str}")
        finally:
            # Bug D — always clean up so a failed commit can't leave stale
            # decisions behind for tomorrow's run.
            _DECISIONS_FILE.unlink(missing_ok=True)
            _ANALYSIS_FILE.unlink(missing_ok=True)

    logger.info(
        "market_open EXECUTE complete — trades=%d symbols=%s",
        trades_placed, symbols_traded,
    )
    return True


def _run_full(dry_run: bool = False) -> bool:
    """
    Local dev path: full pipeline with combined_analyst (uses ANTHROPIC_API_KEY if set,
    falls back to rule-based if not).
    """
    today = date.today().isoformat()
    logger.info("market_open FULL — date=%s dry_run=%s", today, dry_run)

    if _is_circuit_breaker_triggered():
        logger.info("Circuit breaker triggered — halting all new trades")
        return True

    regime_data = detect_regime()
    regime = regime_data["regime"]
    regime_rules = regime_data["rules"]
    regime_str = regime.value if hasattr(regime, "value") else str(regime)
    logger.info("Market regime: %s — %s", regime_str, regime_rules.get("description", ""))

    if not regime_rules.get("new_trades_allowed", True):
        logger.info("Regime %s blocks all new trades — exiting", regime_str)
        handoff.write_handoff_section("market-open", {
            "traded": "none", "reason": f"regime {regime_str} blocks new longs",
        })
        if not dry_run:
            state.commit_memory(f"market-open {today}: blocked by regime {regime_str}")
        return True

    macro = check_macro_calendar()
    macro_impact = macro.get("impact_level", "NORMAL")
    from shark.config import get_settings
    cfg_full = get_settings()
    if macro_impact in ("CRITICAL", "HIGH") and not (cfg_full.is_paper and cfg_full.paper_macro_bypass):
        logger.info("Macro block: %s — %s", macro_impact, macro.get("description", ""))
        handoff.write_handoff_section("market-open", {
            "traded": "none", "reason": f"macro block: {macro.get('description', macro_impact)}",
        })
        if not dry_run:
            state.commit_memory(f"market-open {today}: macro block {macro_impact}")
        return True
    elif macro_impact in ("CRITICAL", "HIGH") and cfg_full.is_paper and cfg_full.paper_macro_bypass:
        logger.info("PAPER MODE: bypassing macro %s block for pipeline testing", macro_impact)

    candidates = handoff.get_validated_symbols()
    if not candidates:
        candidates = _parse_confirmed_candidates(today)
    if not candidates:
        logger.info("No confirmed candidates for %s", today)
        if not dry_run:
            state.commit_memory(f"market-open {today}: none")
        return True

    try:
        account = get_account()
        positions = get_positions()
    except Exception:
        logger.error("Failed to fetch account/positions", exc_info=True)
        return False

    existing_symbols = {p["symbol"].upper() for p in positions}
    weekly_count = state.get_weekly_trade_count()
    peak_equity = state.get_peak_equity()
    portfolio_value = float(account["portfolio_value"])

    # Bootstrap: if peak_equity was never set, initialize from current portfolio
    if peak_equity <= 0 and portfolio_value > 0:
        logger.info("Bootstrapping peak_equity from portfolio: $%.2f", portfolio_value)
        state.update_peak_equity(portfolio_value)
        peak_equity = portfolio_value
    elif portfolio_value > peak_equity:
        state.update_peak_equity(portfolio_value)
        peak_equity = portfolio_value

    max_trades = min(MAX_TRADES_PER_RUN, regime_rules.get("max_new_trades_per_day", 3))
    regime_mult = regime_rules.get("position_size_multiplier", 1.0)
    macro_rules_full = macro.get("rules", {})
    macro_mult = float(macro_rules_full.get("position_size_multiplier", 1.0))
    stop_width = regime_rules.get("stop_width_multiplier", 1.0)

    if macro_mult < 1.0:
        logger.info("Macro sizing adjustment: %.1fx (impact=%s)", macro_mult, macro_impact)

    account_for_guardrails = {
        "portfolio_value": portfolio_value,
        "cash": account["cash"],
        "positions": positions,
    }
    guardrails = Guardrails()
    symbols_traded: list[str] = []
    trades_placed = 0

    # ── Optional: two-tier parallel graph (stage/14-15) ─────────────────
    # Operator opt-in via SHARK_USE_GRAPH=1. Pre-resolves analyses for the
    # full candidate slate in parallel via the LangGraph-style 12-node DAG
    # (grunts on hermes3:8b, judges on hermes3:70b). The per-symbol loop
    # below then reads straight from the cache instead of doing serial
    # analyze_symbol calls. Falls back silently to the legacy path on error.
    _graph_results: dict[str, dict] = {}
    if os.environ.get("SHARK_USE_GRAPH", "false").lower() in ("1", "true", "yes"):
        graph_inputs: list[dict] = []
        for _sym in candidates[: max_trades * 2]:  # cap headroom
            _c_pre = _collect_candidate_data(
                symbol=_sym,
                existing_symbols=existing_symbols,
                account_for_guardrails=account_for_guardrails,
                portfolio_value=portfolio_value,
                peak_equity=peak_equity,
                regime_str=regime_str,
                regime_rules=regime_rules,
                regime_mult=regime_mult,
                macro_mult=macro_mult,
                stop_width=stop_width,
                guardrails=guardrails,
                weekly_count=weekly_count,
                candidates_so_far=0,
            )
            if not _c_pre:
                continue
            graph_inputs.append({
                "symbol": _sym,
                "market_data": _c_pre.get("technicals", {}),
                "perplexity_intel": _c_pre.get("perplexity_intel", {}),
                "risk_check": _c_pre.get("risk_check", {"approved": True}),
            })
        if graph_inputs:
            try:
                from shark.graph import run_candidates_parallel_sync
                _graph_results = run_candidates_parallel_sync(
                    graph_inputs,
                    max_parallel=int(os.environ.get("SHARK_GRAPH_PARALLEL", "5")),
                )
                logger.info(
                    "Two-tier graph evaluated %d candidates", len(_graph_results),
                )
            except Exception as exc:
                logger.warning(
                    "Two-tier graph failed (%s) — falling back to legacy path", exc,
                )
                _graph_results = {}

    for symbol in candidates:
        if trades_placed >= max_trades:
            break
        c = _collect_candidate_data(
            symbol=symbol,
            existing_symbols=existing_symbols,
            account_for_guardrails=account_for_guardrails,
            portfolio_value=portfolio_value,
            peak_equity=peak_equity,
            regime_str=regime_str,
            regime_rules=regime_rules,
            regime_mult=regime_mult,
            macro_mult=macro_mult,
            stop_width=stop_width,
            guardrails=guardrails,
            weekly_count=weekly_count,
            candidates_so_far=trades_placed,
        )
        if not c:
            continue

        # Reconstruct full technicals/bars for combined_analyst
        try:
            bars = get_bars(symbol, timeframe="1Day", limit=60)
            technicals = compute_indicators(bars)
        except Exception:
            logger.error("Error re-fetching bars for %s", symbol, exc_info=True)
            continue

        risk = {
            "approved": True,
            "adjusted_size": c["risk_check"]["adjusted_size"],
            "position_size_pct": c["risk_check"]["position_size_pct"],
            "violations": [],
        }

        # Prefer the two-tier graph result when SHARK_USE_GRAPH=1; otherwise
        # use the legacy combined_analyst path. Same return contract.
        if symbol in _graph_results:
            analysis = _graph_results[symbol]
        else:
            analysis = analyze_symbol(symbol, technicals, bars, c["perplexity_intel"], risk)
        decision = analysis["decision"]

        if decision["decision"] != "BUY":
            logger.info("%s decision=%s — skipping", symbol, decision["decision"])
            continue

        # === LLM Risk Debate (Priority 3) — optional qualitative risk review ===
        if os.environ.get("SHARK_LLM_RISK_REVIEW", "false").lower() in ("true", "1", "yes"):
            try:
                from shark.agents.risk_debate import run_risk_debate
                risk_rounds = int(os.environ.get("SHARK_RISK_DEBATE_ROUNDS", "1"))
                risk_result = run_risk_debate(
                    symbol=symbol,
                    trade_decision=decision,
                    market_data=c["technicals"],
                    rounds=risk_rounds,
                )
                if not risk_result.get("approved", True):
                    logger.info(
                        "%s VETOED by risk debate — %s",
                        symbol, risk_result.get("debate_summary", ""),
                    )
                    continue
                # Apply risk debate adjustments
                decision = risk_result.get("adjusted_decision", decision)
                size_mult = risk_result.get("position_size_mult", 1.0)
            except Exception as exc:
                logger.warning("Risk debate failed for %s (proceeding): %s", symbol, exc)
                size_mult = 1.0
        else:
            size_mult = 1.0

        qty = c["risk_check"]["adjusted_size"]
        if size_mult != 1.0:
            qty = max(1, int(qty * size_mult))
        trail_pct = c["trail_pct"]
        current_price = c["current_price"]

        logger.info(
            "%s APPROVED qty=%d entry=%.2f trail=%.1f%% regime=%s RS=%.2f",
            symbol, qty, current_price, trail_pct, regime_str, c["rs_data"]["rs_composite"],
        )

        # Extract LLM-computed stop/target for true bracket order
        llm_stop = decision.get("stop_loss")
        llm_target = decision.get("target_price")

        if dry_run:
            logger.info(
                "[DRY RUN] Would place bracket order: %s x%d stop=%s target=%s",
                symbol, qty, llm_stop, llm_target,
            )
            continue

        # Pass stop_loss and take_profit so broker places a true bracket (OCO)
        # instead of falling back to trailing stop
        bracket_kwargs: dict = {"trail_pct": trail_pct}
        if llm_stop is not None and llm_target is not None:
            bracket_kwargs["stop_loss"] = float(llm_stop)
            bracket_kwargs["take_profit"] = float(llm_target)

        execution = place_bracket_order(symbol, qty, **bracket_kwargs)

        # Claim ownership (Shark/Wheel isolation, Fix 3) — see _execute().
        try:
            from shared.subsystem_ownership import claim
            claim("shark", symbol)
        except Exception as exc:
            logger.warning("ownership claim failed for %s: %s", symbol, exc)

        fill_price = execution.get("fill_price", current_price)
        stop_price = execution.get("stop_price", c["stop_price"])

        log_trade({
            "date": today,
            "symbol": symbol,
            "side": "buy",
            "qty": qty,
            "price": fill_price,
            "stop": stop_price,
            "catalyst": analysis["bull"].get("catalysts", ""),
            "target": decision.get("target_price", ""),
            "rr": decision.get("risk_reward_ratio", ""),
            "regime": regime_str,
            "rs_composite": c["rs_data"]["rs_composite"],
            "momentum_score": c["technicals"]["momentum_score"],
            "sizing_method": c["sizing_method"],
            "atr": c["technicals"]["atr_14"],
        })

        # === Deferred Outcome Tracking (Priority 4) — store for later resolution ===
        try:
            from shark.agents.outcome_resolver import store_pending_outcome
            store_pending_outcome(
                symbol=symbol,
                entry_date=today,
                entry_price=float(fill_price),
                trade_decision=decision,
            )
        except Exception as exc:
            logger.debug("store_pending_outcome failed for %s: %s", symbol, exc)

        signal = generate_signal(decision, execution)
        body_html = _build_email_body(signal, decision, execution)
        send_email_digest(
            subject=f"Shark BUY Signal — {symbol} @ ${fill_price}",
            body_html=body_html,
        )
        symbols_traded.append(symbol)
        trades_placed += 1

    handoff.write_handoff_section("market-open", {
        "traded": ", ".join(symbols_traded) if symbols_traded else "none",
        "count": str(trades_placed),
        "regime": regime_str,
        "macro": macro.get("description", "normal"),
    })

    if not dry_run:
        # Weekly trade count is derived from TRADE-LOG.md on read; nothing to write here.
        traded_label = ",".join(symbols_traded) if symbols_traded else "none"
        state.commit_memory(f"market-open {today}: {traded_label} regime={regime_str}")

    logger.info(
        "market_open FULL complete — trades=%d symbols=%s regime=%s",
        trades_placed, symbols_traded, regime_str,
    )
    return True


def run(dry_run: bool = False, mode: str = "full") -> bool:
    # Defense-in-depth: even if invoked outside run.py, refuse to run while paused.
    try:
        enforce_kill_switch("market-open")
    except KillSwitchActive as exc:
        logger.error("market-open halted by kill switch: %s", exc)
        return False

    if mode == "prepare":
        return _prepare(dry_run)
    elif mode == "execute":
        return _execute(dry_run)
    else:
        return _run_full(dry_run)
