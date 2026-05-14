"""
Market Regime Detection — classifies the current market environment using SPY.

Four regimes:
  BULL_QUIET   — trending up, low volatility  → full position sizing, aggressive entries
  BULL_VOLATILE — trending up, high volatility → half sizing, wider stops
  BEAR_QUIET   — trending down, low volatility → no new longs, manage existing only
  BEAR_VOLATILE — trending down, high vol       → full defense, tighten everything

Detection method (no ML required — deterministic + robust):
  1. Trend: SPY price vs 50-day SMA + 20/50 SMA crossover
  2. Volatility: 14-day ATR as percentage of price vs 90-day ATR percentile
  3. Breadth confirmation: ratio of watchlist stocks above their own 20-day SMA

LLM annotation (added 2026-05-14):
  After the deterministic classifier produces the regime label, a best-effort
  ``trading-regime-tagger`` LLM call (route in stocks/shark/model_tiers.json,
  defaults to ``hermes3:8b-trader`` on Ollama) produces a one-line natural-
  language ``regime_tag`` + ``regime_narrative``. This is the canonical
  ``regime_tagger`` role the dashboard's AgentFlow courtroom expects to
  fire on every phase — pre-2026-05-14 the role was configured in JSON
  but had NO Python caller, so the courtroom showed "idle" forever (60+
  hours during the 2026-05-12 → 2026-05-14 outage). Failures are silent:
  if Ollama is down the deterministic regime still flows through; the LLM
  is a commentator, never a gatekeeper.
"""

import logging
import os
import time
from datetime import datetime
from enum import Enum
from typing import Any

import pandas as pd

from shark.data.alpaca_data import get_bars

logger = logging.getLogger(__name__)

# In-process cache for the LLM tag — avoids hammering Ollama if detect_regime
# is called multiple times per phase (pre_market + market_open + midday can
# each invoke it). 30-min TTL keeps the tag fresh against the daily cron
# cadence without burning a call on every Python-level re-import.
_LLM_TAG_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "tag": "", "narrative": ""}
_LLM_TAG_CACHE_TTL_S = 1800.0


class MarketRegime(str, Enum):
    BULL_QUIET = "BULL_QUIET"
    BULL_VOLATILE = "BULL_VOLATILE"
    BEAR_QUIET = "BEAR_QUIET"
    BEAR_VOLATILE = "BEAR_VOLATILE"
    UNKNOWN = "UNKNOWN"


# Regime → trading rules
# `min_risk_reward` added 2026-05-14 (stagnant-config audit Wave 1.3) —
# previously hardcoded as 2.0 in three duplicated module constants.
# Volatile regimes demand more cushion; bear regimes don't allow new
# longs at all so the value is academic but kept consistent.
REGIME_RULES: dict[str, dict[str, Any]] = {
    MarketRegime.BULL_QUIET: {
        "new_trades_allowed": True,
        "position_size_multiplier": 1.0,
        "max_new_trades_per_day": 3,
        "stop_width_multiplier": 1.0,
        "confidence_threshold": 0.65,
        "min_risk_reward": 2.0,
        "description": "Trending up, low vol — full aggression",
    },
    MarketRegime.BULL_VOLATILE: {
        "new_trades_allowed": True,
        "position_size_multiplier": 0.5,
        "max_new_trades_per_day": 2,
        "stop_width_multiplier": 1.3,
        "confidence_threshold": 0.75,
        "min_risk_reward": 2.5,
        "description": "Trending up, high vol — half size, wider stops",
    },
    MarketRegime.BEAR_QUIET: {
        "new_trades_allowed": False,
        "position_size_multiplier": 0.0,
        "max_new_trades_per_day": 0,
        "stop_width_multiplier": 1.0,
        "confidence_threshold": 1.0,
        "min_risk_reward": 3.0,
        "description": "Trending down, low vol — NO new longs, manage exits",
    },
    MarketRegime.BEAR_VOLATILE: {
        "new_trades_allowed": False,
        "position_size_multiplier": 0.0,
        "max_new_trades_per_day": 0,
        "stop_width_multiplier": 0.8,
        "confidence_threshold": 1.0,
        "min_risk_reward": 3.0,
        "description": "Trending down, high vol — DEFENSE MODE, tighten everything",
    },
    MarketRegime.UNKNOWN: {
        "new_trades_allowed": True,
        "position_size_multiplier": 0.5,
        "max_new_trades_per_day": 1,
        "stop_width_multiplier": 1.2,
        "confidence_threshold": 0.80,
        "min_risk_reward": 2.2,
        "description": "Cannot determine regime — conservative defaults",
    },
}

# ATR volatility threshold: if ATR% > this, market is "volatile"
_ATR_HIGH_VOL_THRESHOLD = float(os.environ.get("REGIME_ATR_HIGH_VOL_PCT", "1.5"))
_TREND_LOOKBACK = 100  # bars for regime detection (need 50 SMA + cushion)
_BENCHMARK = os.environ.get("REGIME_BENCHMARK", "SPY")


def detect_regime() -> dict[str, Any]:
    """
    Detect current market regime using SPY daily bars.

    Returns:
        Dict with keys:
            regime (MarketRegime): classified regime
            rules (dict): trading rules for this regime
            details (dict): underlying metrics used for classification
            timestamp (str): ISO timestamp of detection
    """
    try:
        bars = get_bars(_BENCHMARK, timeframe="1Day", limit=_TREND_LOOKBACK)
    except Exception as exc:
        logger.error("Failed to fetch %s bars for regime detection: %s", _BENCHMARK, exc)
        return _fallback_result("bars fetch failed")

    if bars is None or (isinstance(bars, pd.DataFrame) and len(bars) < 50):
        return _fallback_result("insufficient bar data")

    df = bars if isinstance(bars, pd.DataFrame) else pd.DataFrame(bars)

    # Normalize column names
    col_map = {"c": "close", "h": "high", "l": "low", "o": "open", "v": "volume"}
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)

    if "close" not in df.columns:
        return _fallback_result("missing close column")

    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else close
    low = df["low"].astype(float) if "low" in df.columns else close

    current_price = float(close.iloc[-1])

    # --- TREND DETECTION ---
    sma_20 = float(close.rolling(20).mean().iloc[-1])
    sma_50 = float(close.rolling(50).mean().iloc[-1])

    # Price position relative to SMAs
    above_sma20 = current_price > sma_20
    above_sma50 = current_price > sma_50
    sma_20_above_50 = sma_20 > sma_50

    # 10-day rate of change (momentum direction)
    roc_10 = (current_price - float(close.iloc[-11])) / float(close.iloc[-11]) * 100 if len(close) > 10 else 0

    # Trend score: -3 to +3
    trend_score = 0
    if above_sma20:
        trend_score += 1
    else:
        trend_score -= 1
    if above_sma50:
        trend_score += 1
    else:
        trend_score -= 1
    if sma_20_above_50:
        trend_score += 1
    else:
        trend_score -= 1

    is_bullish = trend_score >= 1  # at least 2 of 3 bullish signals

    # --- VOLATILITY DETECTION ---
    # ATR-14 as percentage of price
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr_14 = float(tr.rolling(14).mean().iloc[-1])
    atr_pct = (atr_14 / current_price) * 100

    # Compare current ATR to its 90-day range for percentile
    atr_series = tr.rolling(14).mean().dropna()
    if len(atr_series) >= 20:
        atr_percentile = float((atr_series < atr_14).mean() * 100)
    else:
        atr_percentile = 50.0

    is_high_vol = atr_pct > _ATR_HIGH_VOL_THRESHOLD or atr_percentile > 75

    # --- VIX PROXY: intraday range expansion ---
    # If avg daily range (H-L)/C over last 5 days > 2%, that's volatile
    if len(df) >= 5:
        recent_range = float(((high.iloc[-5:] - low.iloc[-5:]) / close.iloc[-5:]).mean() * 100)
    else:
        recent_range = 0.0

    if recent_range > 2.5:
        is_high_vol = True

    # --- CLASSIFY ---
    if is_bullish and not is_high_vol:
        regime = MarketRegime.BULL_QUIET
    elif is_bullish and is_high_vol:
        regime = MarketRegime.BULL_VOLATILE
    elif not is_bullish and not is_high_vol:
        regime = MarketRegime.BEAR_QUIET
    else:
        regime = MarketRegime.BEAR_VOLATILE

    details = {
        "benchmark": _BENCHMARK,
        "price": round(current_price, 2),
        "sma_20": round(sma_20, 2),
        "sma_50": round(sma_50, 2),
        "above_sma20": above_sma20,
        "above_sma50": above_sma50,
        "sma_20_above_50": sma_20_above_50,
        "trend_score": trend_score,
        "roc_10": round(roc_10, 2),
        "atr_14": round(atr_14, 2),
        "atr_pct": round(atr_pct, 2),
        "atr_percentile": round(atr_percentile, 1),
        "recent_range_pct": round(recent_range, 2),
        "is_bullish": is_bullish,
        "is_high_vol": is_high_vol,
    }

    rules = REGIME_RULES[regime]

    # Paper-mode override: allow limited trading in BEAR regimes
    from shark.config import get_settings
    cfg = get_settings()
    if cfg.is_paper and cfg.paper_bear_override and regime in (
        MarketRegime.BEAR_QUIET, MarketRegime.BEAR_VOLATILE,
    ):
        # Keep the original stop_width from the regime, override trade permissions
        base_stop_width = rules["stop_width_multiplier"]
        rules = {
            "new_trades_allowed": True,
            "position_size_multiplier": cfg.paper_bear_size_mult,
            "max_new_trades_per_day": cfg.paper_bear_max_trades,
            "stop_width_multiplier": base_stop_width,
            "confidence_threshold": cfg.paper_bear_confidence,
            "description": (
                f"PAPER MODE — {regime.value} override: "
                f"{cfg.paper_bear_max_trades} trade/day, "
                f"{cfg.paper_bear_size_mult}x size, "
                f"{cfg.paper_bear_confidence} confidence"
            ),
        }
        logger.info(
            "PAPER MODE: overriding %s rules — %d trade/day at %.1fx size (confidence ≥ %.2f)",
            regime.value, cfg.paper_bear_max_trades,
            cfg.paper_bear_size_mult, cfg.paper_bear_confidence,
        )

    logger.info(
        "Market regime: %s | trend_score=%d atr_pct=%.2f%% atr_pctl=%.0f%% | %s",
        regime.value, trend_score, atr_pct, atr_percentile, rules["description"],
    )

    # ── LLM regime tagger (best-effort, never gates the deterministic regime) ──
    # The deterministic classifier above is the source of truth for trading
    # decisions. The LLM call below is purely a commentator — it produces a
    # one-line natural-language tag the dashboard's AgentFlow courtroom can
    # display. Wired here (not in each phase) so EVERY caller of
    # detect_regime gets the same per-phase LLM heartbeat, keeping the
    # ``regime_tagger`` role from going stagnant when there are no closed
    # equity trades or no candidates clear the catalyst gate.
    llm_tag, llm_narrative = _llm_annotate_regime(regime, details)

    return {
        "regime": regime,
        "rules": rules,
        "details": details,
        "regime_tag": llm_tag,
        "regime_narrative": llm_narrative,
        "timestamp": datetime.now().isoformat(),
    }


def _llm_annotate_regime(regime: MarketRegime, details: dict) -> tuple[str, str]:
    """Best-effort LLM annotation of the regime.

    Returns ``(tag, narrative)`` strings. On any failure (Ollama down,
    timeout, JSON parse error) returns ``("", "")`` — the deterministic
    regime is the source of truth, so the caller's flow is untouched.

    Cached for ``_LLM_TAG_CACHE_TTL_S`` to avoid burning a call on every
    repeated detect_regime() invocation within a phase. Cache key includes
    the deterministic regime + trend score + ATR percentile bucket so a
    legitimate regime flip invalidates the cache immediately.
    """
    now = time.time()
    cache_key = f"{regime.value}|{details.get('trend_score')}|{int(details.get('atr_percentile', 0) // 5)}"
    if (
        now - _LLM_TAG_CACHE["ts"] < _LLM_TAG_CACHE_TTL_S
        and _LLM_TAG_CACHE["key"] == cache_key
        and _LLM_TAG_CACHE["tag"]
    ):
        return _LLM_TAG_CACHE["tag"], _LLM_TAG_CACHE["narrative"]

    # Disable knob — operator can set SHARK_REGIME_TAGGER_DISABLED=1 to skip
    # the LLM call entirely (e.g. when Ollama is being maintained).
    if os.environ.get("SHARK_REGIME_TAGGER_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return "", ""

    try:
        from shark.llm.client import chat_by_role
        system = (
            "You are a market-regime tagger. Given a deterministic regime "
            "label plus underlying metrics, output a one-phrase TAG (2-5 "
            "words) and a one-sentence NARRATIVE describing what the regime "
            "MEANS for a momentum-tilted long-only stock book. Return ONLY "
            "valid JSON with keys 'tag' and 'narrative'. No prose outside JSON."
        )
        user = (
            f"Deterministic regime: {regime.value}\n"
            f"Trend score (-3 bearish .. +3 bullish): {details.get('trend_score')}\n"
            f"ATR%: {details.get('atr_pct')} | ATR percentile: {details.get('atr_percentile')}\n"
            f"Price vs SMA20: {details.get('above_sma20')} | vs SMA50: {details.get('above_sma50')}\n"
            f"Recent range%: {details.get('recent_range_pct')}\n"
            f"\n"
            f"Return JSON: {{\"tag\": \"<2-5 word phrase>\", \"narrative\": \"<one sentence>\"}}"
        )
        raw, _usage, _model = chat_by_role(
            role="trading-regime-tagger",
            system_prompt=system,
            user_message=user,
            max_tokens=150,
            temperature=0.2,
            agent="regime_tagger",
            json_mode=True,
        )
        # The router returns whatever the model produced; tolerate code fences.
        text = (raw or "").strip()
        if text.startswith("```"):
            text = "\n".join(
                ln for ln in text.splitlines() if not ln.startswith("```")
            ).strip()
        import json as _json
        try:
            obj = _json.loads(text)
        except _json.JSONDecodeError:
            # Some models emit a leading sentence before the JSON — last-resort
            # brace-scan recovery. If even that fails, return empty.
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    obj = _json.loads(text[start : end + 1])
                except _json.JSONDecodeError:
                    return "", ""
            else:
                return "", ""
        tag = str(obj.get("tag", "")).strip()[:60]
        narrative = str(obj.get("narrative", "")).strip()[:240]
        if tag:
            _LLM_TAG_CACHE["ts"] = now
            _LLM_TAG_CACHE["key"] = cache_key
            _LLM_TAG_CACHE["tag"] = tag
            _LLM_TAG_CACHE["narrative"] = narrative
        return tag, narrative
    except Exception as exc:  # pragma: no cover — best-effort path
        logger.debug("LLM regime tag failed: %s — continuing without it", exc)
        return "", ""


def get_regime_rules(regime: MarketRegime) -> dict[str, Any]:
    """Return trading rules for a given regime."""
    return REGIME_RULES.get(regime, REGIME_RULES[MarketRegime.UNKNOWN])


def _fallback_result(reason: str) -> dict[str, Any]:
    logger.warning("Regime detection fallback: %s — using UNKNOWN (conservative)", reason)
    return {
        "regime": MarketRegime.UNKNOWN,
        "rules": REGIME_RULES[MarketRegime.UNKNOWN],
        "details": {"error": reason},
        "timestamp": datetime.now().isoformat(),
    }
