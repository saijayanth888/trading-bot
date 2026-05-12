"""
LLM-driven indicator selection agent.

Pattern (Apache-2.0): adapted from TradingAgents
`agents/analysts/market_analyst.py`. The original picks ≤8 non-redundant
TA indicators per ticker for an LLM-driven trading workflow. We reuse the
same candidate list + redundancy guidance, but:

  - Inputs: ticker + regime + last-20-bar OHLCV summary (no live tools).
  - Output: validated `IndicatorSelection` Pydantic schema.
  - Routing: hermes3:8b via shark.llm.client.chat_json (cheap; per-pair-per-day).
  - Cache: disk-backed at stocks/kb/indicator_selection/<TICKER>_<REGIME>_<DATE>.json
    with a 24h TTL keyed by (ticker, regime, date). Regime change for the same
    ticker invalidates the previous day's file (different cache key).

Why a separate agent?
  - Strategies (FreqAIMeanRevV1 / BollingerRSI MR) currently hardcode their
    feature lists. Different regimes deserve different feature emphasis
    (trending → SMA/EMA + MACD, mean-reverting → Bollinger + RSI). Letting an
    LLM curate ≤8 non-redundant indicators per (ticker, regime) gives feature
    selection that adapts without retraining.

This module produces *recommendations*; wiring into FreqAI is a separate
task — see `shark/data/indicator_selection.py` for the strategy-facing
helper, and HANDOFF.md for the wiring spec.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import time
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Candidate indicators — copied from TradingAgents structure (Apache-2.0).
# Each entry: stable id (matches stockstats / freqtrade convention) + the
# usage hint that goes into the system prompt verbatim.
# ---------------------------------------------------------------------------

CANDIDATE_INDICATORS: list[dict[str, str]] = [
    # Moving averages — trend
    {
        "indicator": "close_50_sma",
        "category": "moving_average",
        "hint": (
            "50 SMA — medium-term trend. Identify direction + dynamic "
            "support/resistance. Lags price; pair with faster signals for entries."
        ),
    },
    {
        "indicator": "close_200_sma",
        "category": "moving_average",
        "hint": (
            "200 SMA — long-term trend benchmark. Confirm overall trend, "
            "spot golden/death cross. Reacts slowly; strategic confirmation only."
        ),
    },
    {
        "indicator": "close_10_ema",
        "category": "moving_average",
        "hint": (
            "10 EMA — responsive short-term average. Capture momentum shifts and "
            "pullback entries. Noisy in chop; filter with longer averages."
        ),
    },
    {
        "indicator": "close_20_ema",
        "category": "moving_average",
        "hint": (
            "20 EMA — short-to-medium momentum. Common pullback entry trigger; "
            "pairs well with Bollinger middle as confirmation, not duplication."
        ),
    },
    # MACD family
    {
        "indicator": "macd",
        "category": "momentum",
        "hint": (
            "MACD line — EMA differential momentum. Watch crossovers + divergence "
            "for trend changes. Confirm with another tool in low-vol regimes."
        ),
    },
    {
        "indicator": "macd_signal",
        "category": "momentum",
        "hint": (
            "MACD signal — EMA smoothing of MACD. Crossovers with MACD line trigger "
            "trades. Use as part of a broader strategy to suppress false positives."
        ),
    },
    {
        "indicator": "macd_hist",
        "category": "momentum",
        "hint": (
            "MACD histogram — gap between MACD and signal. Visualises momentum "
            "strength + early divergence. Volatile; complement with filters."
        ),
    },
    # Momentum oscillator
    {
        "indicator": "rsi",
        "category": "oscillator",
        "hint": (
            "RSI — momentum oscillator flagging overbought (>70) and oversold (<30). "
            "Watch divergence near reversals. In strong trends RSI rides extremes."
        ),
    },
    # Volatility / mean-reversion
    {
        "indicator": "boll",
        "category": "volatility",
        "hint": (
            "Bollinger middle — 20 SMA basis for the bands. Dynamic benchmark for "
            "price; combine with upper/lower bands to spot breakouts or reversals."
        ),
    },
    {
        "indicator": "boll_ub",
        "category": "volatility",
        "hint": (
            "Bollinger upper — 2 sigma above middle. Overbought / breakout zone. "
            "Confirm with another signal; price can ride the band in strong trends."
        ),
    },
    {
        "indicator": "boll_lb",
        "category": "volatility",
        "hint": (
            "Bollinger lower — 2 sigma below middle. Oversold / mean-reversion zone. "
            "Validate with momentum to avoid catching falling knives."
        ),
    },
    {
        "indicator": "atr",
        "category": "volatility",
        "hint": (
            "ATR — average true range. Size stops + position size to current vol. "
            "Reactive; use within a broader risk framework."
        ),
    },
    # Volume-weighted
    {
        "indicator": "vwma",
        "category": "volume",
        "hint": (
            "VWMA — volume-weighted moving average. Confirms trend by weighting "
            "price by participation. Beware volume spikes skewing the line."
        ),
    },
]

VALID_INDICATOR_IDS: set[str] = {c["indicator"] for c in CANDIDATE_INDICATORS}
MAX_PICKS = 8


# ---------------------------------------------------------------------------
# Pydantic schema — validated output contract.
# ---------------------------------------------------------------------------


class IndicatorPick(BaseModel):
    """One indicator pick + its 1-sentence usage rationale."""

    indicator: str = Field(
        description="Stable indicator id from the candidate list (e.g. close_50_sma)."
    )
    why: str = Field(
        description="One-sentence usage note: when this indicator matters in this regime."
    )

    @field_validator("indicator")
    @classmethod
    def _check_known(cls, v: str) -> str:
        if v not in VALID_INDICATOR_IDS:
            raise ValueError(
                f"unknown indicator '{v}'; must be one of "
                f"{sorted(VALID_INDICATOR_IDS)}"
            )
        return v


class IndicatorSelection(BaseModel):
    """Full selection result. Capped at MAX_PICKS, no duplicates."""

    ticker: str
    regime: str
    picks: list[IndicatorPick] = Field(default_factory=list)

    @field_validator("picks")
    @classmethod
    def _check_picks(cls, v: list[IndicatorPick]) -> list[IndicatorPick]:
        if len(v) > MAX_PICKS:
            # Truncate rather than raise — the system prompt asks for ≤8 but
            # an over-eager LLM shouldn't fail the whole call.
            v = v[:MAX_PICKS]
        # De-dup while preserving order
        seen: set[str] = set()
        deduped: list[IndicatorPick] = []
        for p in v:
            if p.indicator in seen:
                continue
            seen.add(p.indicator)
            deduped.append(p)
        return deduped


# ---------------------------------------------------------------------------
# Cache helpers — disk only, never in-memory. 24h TTL.
# ---------------------------------------------------------------------------

CACHE_TTL_SECONDS = 24 * 3600


def _kb_root() -> Path:
    """Resolve the kb root regardless of where Python was launched.

    Walks up from this file looking for a `stocks/kb` directory. Falls back
    to env var SHARK_KB_DIR for tests / containerized runs.
    """
    env = os.environ.get("SHARK_KB_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "stocks" / "kb"
        if candidate.exists():
            return candidate
        candidate = parent / "kb"
        if candidate.exists() and candidate.parent.name == "stocks":
            return candidate
    # Fallback: assume cwd has stocks/kb
    return Path.cwd() / "stocks" / "kb"


def _cache_path(ticker: str, regime: str, on_date: _date | None = None) -> Path:
    """Per-(ticker, regime, date) cache file path."""
    when = on_date or _date.today()
    safe_ticker = ticker.replace("/", "_").upper()
    safe_regime = regime.replace("/", "_").upper()
    root = _kb_root() / "indicator_selection"
    return root / f"{safe_ticker}_{safe_regime}_{when.isoformat()}.json"


def _read_cache(path: Path) -> IndicatorSelection | None:
    """Return cached selection if file exists, valid JSON, and within TTL."""
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > CACHE_TTL_SECONDS:
            logger.debug("cache stale (%ds): %s", int(age), path)
            return None
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return IndicatorSelection.model_validate(data)
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
        logger.warning("cache read failed for %s: %s", path, exc)
        return None


def _write_cache(path: Path, selection: IndicatorSelection) -> None:
    """Atomically write selection to cache."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(selection.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("cache write failed for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# OHLCV summary — keep prompt small.
# ---------------------------------------------------------------------------


@dataclass
class OHLCVBar:
    """Minimal OHLCV bar shape used by `summarize_bars`."""
    o: float
    h: float
    l: float
    c: float
    v: float


def _coerce_bar(b: Any) -> OHLCVBar | None:
    """Best-effort conversion from common dict shapes to OHLCVBar."""
    if b is None:
        return None
    try:
        if isinstance(b, dict):
            o = float(b.get("o", b.get("open", 0)))
            h = float(b.get("h", b.get("high", 0)))
            l = float(b.get("l", b.get("low", 0)))
            c = float(b.get("c", b.get("close", 0)))
            v = float(b.get("v", b.get("volume", 0)))
        else:
            return None
        return OHLCVBar(o=o, h=h, l=l, c=c, v=v)
    except (TypeError, ValueError):
        return None


def summarize_bars(bars: Iterable[Any], lookback: int = 20) -> dict[str, float]:
    """Compress the last `lookback` bars into a small stats dict for the prompt.

    We deliberately do NOT compute traditional indicators here — that would
    bias the LLM towards picking what we precomputed. We give it raw stats:
    range, return, vol, current vs SMA, volume ratio. The LLM then chooses
    what to track *going forward*.
    """
    coerced = [b for b in (_coerce_bar(x) for x in bars) if b is not None]
    if not coerced:
        return {}
    window = coerced[-lookback:]
    closes = [b.c for b in window]
    highs = [b.h for b in window]
    lows = [b.l for b in window]
    vols = [b.v for b in window]
    if not closes:
        return {}
    last = closes[-1]
    first = closes[0]
    pct_change = (last - first) / first if first else 0.0
    rng = (max(highs) - min(lows)) / last if last else 0.0
    sma = sum(closes) / len(closes)
    realized_vol = (
        statistics.pstdev([
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1]
        ])
        if len(closes) > 1
        else 0.0
    )
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    return {
        "bars_observed": len(window),
        "current_close": round(last, 4),
        "pct_change_window": round(pct_change, 4),
        "high_low_range_pct": round(rng, 4),
        "sma_window": round(sma, 4),
        "current_vs_sma_pct": round((last - sma) / sma if sma else 0.0, 4),
        "realized_vol_pct": round(realized_vol, 4),
        "avg_volume": round(avg_vol, 2),
        "current_vs_avg_volume": round(
            (vols[-1] / avg_vol) if avg_vol else 1.0, 2
        ),
    }


# ---------------------------------------------------------------------------
# Prompt construction.
# ---------------------------------------------------------------------------


def _build_indicator_menu() -> str:
    """Render the candidate indicator list for the system prompt."""
    lines = []
    by_cat: dict[str, list[dict[str, str]]] = {}
    for c in CANDIDATE_INDICATORS:
        by_cat.setdefault(c["category"], []).append(c)
    for cat, items in by_cat.items():
        lines.append(f"\n{cat.upper()}:")
        for item in items:
            lines.append(f"  - {item['indicator']}: {item['hint']}")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are a disciplined quantitative trading analyst. Your job: pick the "
    f"≤{MAX_PICKS} most useful technical indicators for trading the given ticker "
    "in the current market regime. Each pick must include a one-sentence USAGE "
    "NOTE explaining when it matters in this regime. "
    "Hard rules: "
    "(1) NEVER pick more than "
    f"{MAX_PICKS} indicators. "
    "(2) NEVER pick two redundant indicators (e.g. close_50_sma + close_200_sma "
    "are both trend SMAs at similar lengths — pick one; rsi alone is preferred over "
    "rsi + a second momentum oscillator; macd line + macd_signal is OK because they "
    "trigger together; macd_hist is redundant with macd line, only pick if you skip macd). "
    "(3) Match selection to regime: trending regimes lean on moving averages + MACD; "
    "mean-reverting / volatile regimes lean on Bollinger + RSI + ATR. "
    "(4) Indicator ids MUST come from the menu below — do not invent new ones. "
    "(5) Output ONLY a single JSON object with key `picks` whose value is an "
    "array of `{\"indicator\": <id>, \"why\": <one sentence>}` objects. No prose, "
    "no markdown fences, no commentary outside the JSON.\n\n"
    "Indicator menu:\n" + _build_indicator_menu()
)


def _build_user_prompt(
    ticker: str,
    regime: str,
    bar_summary: dict[str, float],
) -> str:
    return (
        f"Ticker: {ticker}\n"
        f"Regime: {regime}\n"
        f"Recent OHLCV summary (last {bar_summary.get('bars_observed', 0)} bars):\n"
        f"```json\n{json.dumps(bar_summary, indent=2)}\n```\n\n"
        f"Pick the ≤{MAX_PICKS} non-redundant indicators best suited to trade "
        f"{ticker} in a `{regime}` regime. Return JSON only."
    )


# ---------------------------------------------------------------------------
# JSON parsing — robust to LLM quirks (code fences, extra text, etc.).
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    """Strip markdown code fences if the LLM ignored 'no fences' instruction."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.startswith("```")).strip()
    return text


def _extract_json_object(text: str) -> str:
    """Pull the outermost JSON value (`{...}` or `[...]`) out of noisy text.

    Picks whichever opener appears first and pairs it with the matching
    closer at the rightmost position. Falls back to the original text if
    nothing looks JSON-shaped.
    """
    text = _strip_fences(text)
    obj_start = text.find("{")
    arr_start = text.find("[")
    # Pick the earliest opener that's actually present
    candidates = [s for s in (obj_start, arr_start) if s != -1]
    if not candidates:
        return text
    start = min(candidates)
    closer = "}" if text[start] == "{" else "]"
    end = text.rfind(closer)
    if end <= start:
        return text
    return text[start : end + 1]


def parse_selection(
    raw: str, ticker: str, regime: str
) -> IndicatorSelection:
    """Parse + validate raw LLM output into IndicatorSelection.

    Tolerates: code fences, leading/trailing prose, picks-list-only output
    (no wrapping object), unknown indicator ids (filtered out), >8 picks
    (truncated by validator).
    """
    text = _extract_json_object(raw or "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("indicator selection JSON parse failed: %s", exc)
        return IndicatorSelection(ticker=ticker, regime=regime, picks=[])

    if isinstance(parsed, list):
        picks_raw = parsed
    elif isinstance(parsed, dict):
        picks_raw = parsed.get("picks") or parsed.get("indicators") or []
    else:
        picks_raw = []

    picks: list[IndicatorPick] = []
    for entry in picks_raw:
        if not isinstance(entry, dict):
            continue
        indicator = entry.get("indicator") or entry.get("name") or ""
        why = entry.get("why") or entry.get("usage") or entry.get("reason") or ""
        if indicator not in VALID_INDICATOR_IDS:
            logger.debug("dropping unknown indicator: %s", indicator)
            continue
        try:
            picks.append(IndicatorPick(indicator=indicator, why=why or ""))
        except ValidationError as exc:
            logger.debug("dropping invalid pick %s: %s", entry, exc)
            continue

    return IndicatorSelection(ticker=ticker, regime=regime, picks=picks)


# ---------------------------------------------------------------------------
# Defaults — used when LLM is unavailable AND no cache is fresh.
# ---------------------------------------------------------------------------

# Conservative regime → indicator map. Picks chosen to be non-redundant and
# cover trend + momentum + volatility within MAX_PICKS.
_DEFAULT_PICKS: dict[str, list[tuple[str, str]]] = {
    "trending_up": [
        ("close_50_sma", "Confirms medium-term uptrend; price above is bullish."),
        ("close_20_ema", "Pullback entry trigger in an established uptrend."),
        ("macd", "Trend continuation + bearish-divergence early warning."),
        ("macd_signal", "Crossover trigger paired with MACD line."),
        ("rsi", "Flag overbought extremes that often precede pullbacks."),
        ("atr", "Size trailing stops to current realised volatility."),
        ("vwma", "Confirm move with participation; weak volume = suspect rally."),
    ],
    "trending_down": [
        ("close_50_sma", "Confirms medium-term downtrend; price below is bearish."),
        ("close_20_ema", "Bounce-fade entry trigger in an established downtrend."),
        ("macd", "Continuation + bullish-divergence early warning."),
        ("macd_signal", "Crossover trigger paired with MACD line."),
        ("rsi", "Flag oversold extremes that may precede relief rallies."),
        ("atr", "Size short stops to current volatility."),
        ("vwma", "Confirm distribution with volume; thin selling = trap."),
    ],
    "mean_reverting": [
        ("boll", "Mean reference for a range-bound market."),
        ("boll_ub", "Upper band — sell-side reversion zone."),
        ("boll_lb", "Lower band — buy-side reversion zone."),
        ("rsi", "Confirm overbought/oversold at band touches."),
        ("atr", "Filter trades when range collapses or expands abruptly."),
        ("close_20_ema", "Local trend filter to avoid fading a regime change."),
    ],
    "bear_volatile": [
        ("close_200_sma", "Long-term bearish bias filter; only short below."),
        ("boll_ub", "Volatile bounces back to upper band fade."),
        ("boll_lb", "Capitulation marker; reduce shorts at lower band."),
        ("rsi", "Catch oversold capitulation rallies; avoid chasing."),
        ("atr", "Vol expansion = wider stops; protect from whipsaw."),
        ("macd_hist", "Momentum acceleration / deceleration in chop."),
    ],
    "high_volatility": [
        ("atr", "Primary regime tag — size everything to ATR."),
        ("boll_ub", "Vol-band reversion zone for shorts."),
        ("boll_lb", "Vol-band reversion zone for longs."),
        ("rsi", "Extremes more reliable when realised vol is high."),
        ("macd", "Filter direction so we only fade in the trend's favour."),
        ("close_50_sma", "Anchor for whether vol expansion is a trend or chop."),
    ],
}

_DEFAULT_PICKS["uptrend"] = _DEFAULT_PICKS["trending_up"]
_DEFAULT_PICKS["downtrend"] = _DEFAULT_PICKS["trending_down"]
_DEFAULT_PICKS["BEAR_VOLATILE"] = _DEFAULT_PICKS["bear_volatile"]


def _default_selection(ticker: str, regime: str) -> IndicatorSelection:
    """Deterministic fallback when LLM is down."""
    key_lower = (regime or "").lower()
    picks_seed = (
        _DEFAULT_PICKS.get(regime)
        or _DEFAULT_PICKS.get(key_lower)
        or _DEFAULT_PICKS["mean_reverting"]
    )
    picks = [IndicatorPick(indicator=ind, why=why) for ind, why in picks_seed[:MAX_PICKS]]
    return IndicatorSelection(ticker=ticker, regime=regime, picks=picks)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def select_indicators(
    ticker: str,
    regime: str,
    bars: Iterable[Any] | None = None,
    *,
    use_cache: bool = True,
    on_date: _date | None = None,
    chat_json_fn: Any | None = None,
) -> IndicatorSelection:
    """Select ≤8 non-redundant indicators for (ticker, regime).

    Cache is keyed by (ticker, regime, date) — a regime change for the same
    ticker on the same day yields a different cache file, so the LLM is
    re-queried for the new regime. TTL is 24h (intra-day reuse is the goal).

    Args:
        ticker: Symbol (e.g. "NVDA", "BTC/USDT").
        regime: Regime label (e.g. "trending_up", "BEAR_VOLATILE").
        bars: Optional OHLCV bars for the prompt (last 20 used). May be None
              — selection will still proceed using regime + ticker only.
        use_cache: If True, read/write the disk cache. Tests pass False.
        on_date: Override "today" (testing).
        chat_json_fn: Inject a fake LLM in tests. Defaults to
                      shark.llm.client.chat_json.

    Returns:
        Validated IndicatorSelection. Always returns *something* — falls back
        to a regime-default if the LLM is unreachable.
    """
    cache_path = _cache_path(ticker, regime, on_date=on_date)
    if use_cache:
        cached = _read_cache(cache_path)
        if cached is not None:
            logger.debug("indicator selection cache hit: %s", cache_path)
            return cached

    bar_summary = summarize_bars(bars or [])
    user_prompt = _build_user_prompt(ticker, regime, bar_summary)

    if chat_json_fn is None:
        try:
            from shark.llm.client import chat_json as _chat_json
            chat_json_fn = _chat_json
        except Exception as exc:
            logger.warning("LLM client import failed (%s) — using defaults", exc)
            sel = _default_selection(ticker, regime)
            if use_cache:
                _write_cache(cache_path, sel)
            return sel

    raw = ""
    try:
        raw, _usage, _model = chat_json_fn(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_prompt,
            max_tokens=600,
            temperature=0.2,
            tier="fast",
            agent="market_analyst",
        )
    except Exception as exc:
        logger.warning(
            "indicator-selection LLM call failed for %s/%s: %s — falling back",
            ticker, regime, exc,
        )
        sel = _default_selection(ticker, regime)
        if use_cache:
            _write_cache(cache_path, sel)
        return sel

    selection = parse_selection(raw, ticker, regime)
    if not selection.picks:
        logger.warning(
            "LLM returned no valid picks for %s/%s — using regime defaults",
            ticker, regime,
        )
        selection = _default_selection(ticker, regime)

    if use_cache:
        _write_cache(cache_path, selection)
    return selection
