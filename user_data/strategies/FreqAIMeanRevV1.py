"""
FreqAIMeanRevV1 — starter FreqAI strategy.

Trains a LightGBM classifier to predict whether the close `label_period_candles`
ahead will be higher ("up") or lower ("down") than the current close. Uses RSI,
MACD, Bollinger bands, volume SMA ratio and ATR as features.
"""

import logging
import sys
from functools import reduce
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
from pandas import DataFrame
import talib.abstract as ta

import freqtrade.vendor.qtpylib.indicators as qtpylib
from freqtrade.strategy import DecimalParameter, IStrategy

logger = logging.getLogger(__name__)

# Make user_data/modules importable from the strategies/ directory.
_USER_DATA = Path(__file__).resolve().parent.parent
if str(_USER_DATA) not in sys.path:
    sys.path.insert(0, str(_USER_DATA))

try:
    from modules.onchain_signals import (
        FEATURE_COLUMNS as ONCHAIN_FEATURES,
        get_features as get_onchain_features,
    )
    _ONCHAIN_AVAILABLE = True
except Exception as exc:
    logger.warning("on-chain module unavailable, features will be neutral: %s", exc)
    ONCHAIN_FEATURES = (
        "%-onchain_netflow_z",
        "%-onchain_mvrv",
        "%-onchain_whale_count_1h",
        "%-onchain_whale_volume_1h",
    )
    get_onchain_features = None
    _ONCHAIN_AVAILABLE = False

try:
    from modules.sentiment_engine import (
        FEATURE_COLUMNS as SENTIMENT_FEATURES,
        get_sentiment_features,
    )
    _SENTIMENT_AVAILABLE = True
except Exception as exc:
    logger.warning("sentiment module unavailable, features will be neutral: %s", exc)
    SENTIMENT_FEATURES = (
        "%-sentiment_score",
        "%-sentiment_confidence",
        "%-sentiment_bullish",
        "%-sentiment_bearish",
        "%-sentiment_agreement",
    )
    get_sentiment_features = None
    _SENTIMENT_AVAILABLE = False

try:
    from modules.regime_detector import (
        FEATURE_COLUMNS as REGIME_FEATURES,
        REGIME_LABELS,
        get_regime_features,
    )
    _REGIME_AVAILABLE = True
except Exception as exc:
    logger.warning("regime module unavailable, features will be neutral: %s", exc)
    REGIME_LABELS = ("trending_up", "trending_down", "mean_reverting", "high_volatility")
    REGIME_FEATURES = tuple(
        [f"%-regime_is_{l}" for l in REGIME_LABELS]
        + [f"%-regime_prob_{l}" for l in REGIME_LABELS]
        + ["%-regime_probability", "%-regime_duration_h"]
    )
    get_regime_features = None
    _REGIME_AVAILABLE = False

try:
    from modules.drl_ensemble import DEFAULT_SAVE_DIR as DRL_SAVE_DIR, DRLEnsemble
    from modules.ensemble_voter import vote_batch
    from modules.meta_agent import compute_signal as meta_compute_signal
    _DRL_AVAILABLE = True
except Exception as exc:
    logger.warning("DRL ensemble unavailable, meta-agent will fall back to TFT: %s", exc)
    DRLEnsemble = None
    vote_batch = None
    meta_compute_signal = None
    DRL_SAVE_DIR = Path("user_data/models/drl")
    _DRL_AVAILABLE = False

try:
    from modules.risk_governor import RiskGovernor
    _RISK_AVAILABLE = True
except Exception as exc:
    logger.warning("risk_governor unavailable, trades will NOT be gated: %s", exc)
    RiskGovernor = None
    _RISK_AVAILABLE = False

# Monitoring (Slack alerts + trade journal + Influx metrics) lives in a
# mixin so this file can stay focused on signal logic. The mixin handles
# its own graceful-degradation (no-op if any monitoring module fails).
from modules.monitoring_mixin import MonitoringMixin


_ONCHAIN_NEUTRAL = {
    "%-onchain_netflow_z": 0.0,
    "%-onchain_mvrv": 1.0,
    "%-onchain_whale_count_1h": 0.0,
    "%-onchain_whale_volume_1h": 0.0,
}

_SENTIMENT_NEUTRAL = {col: 0.0 for col in SENTIMENT_FEATURES}

_REGIME_NEUTRAL = {col: 0.0 for col in REGIME_FEATURES}


def _normalize_dt_index(df: DataFrame) -> DataFrame:
    """
    Force a tz-aware DatetimeIndex to millisecond resolution so it matches
    the `date` column Freqtrade puts in the candle dataframe. Pandas 3.0
    refuses to merge_asof across resolutions, and our Postgres-backed
    feature dataframes come back at microsecond precision by default.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        return df
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    try:
        df.index = df.index.astype("datetime64[ms, UTC]")
    except Exception:
        # Fallback for older pandas: floor to millisecond and re-localize
        df.index = df.index.floor("ms")
    return df


def _normalize_dt_column(df: DataFrame, col: str = "date") -> DataFrame:
    """Same as `_normalize_dt_index` for a Series column (left side of merge_asof)."""
    if col not in df.columns:
        return df
    s = pd.to_datetime(df[col], utc=True)
    try:
        df[col] = s.astype("datetime64[ms, UTC]")
    except Exception:
        df[col] = s.dt.floor("ms")
    return df


def _attach_onchain(dataframe: DataFrame, pair: str) -> DataFrame:
    """Merge on-chain features onto a candle dataframe (1h cadence, ffill)."""
    onchain = None
    if _ONCHAIN_AVAILABLE and get_onchain_features is not None:
        try:
            onchain = get_onchain_features(pair, "1h")
        except Exception as exc:
            logger.warning("on-chain fetch failed for %s: %s", pair, exc)
            onchain = None

    if onchain is None or onchain.empty:
        for col, default in _ONCHAIN_NEUTRAL.items():
            dataframe[col] = default
        return dataframe

    df_sorted = _normalize_dt_column(dataframe.sort_values("date").reset_index(drop=True))
    onchain_sorted = _normalize_dt_index(onchain.sort_index())
    merged = pd.merge_asof(
        df_sorted, onchain_sorted,
        left_on="date", right_index=True,
        direction="backward",
    )
    for col, default in _ONCHAIN_NEUTRAL.items():
        if col not in merged.columns:
            merged[col] = default
        else:
            merged[col] = merged[col].fillna(default)
    return merged


def _attach_sentiment(dataframe: DataFrame, pair: str) -> DataFrame:
    """Merge LLM sentiment features (15-min cadence) onto a candle dataframe."""
    sentiment = None
    if _SENTIMENT_AVAILABLE and get_sentiment_features is not None:
        try:
            sentiment = get_sentiment_features(pair)
        except Exception as exc:
            logger.warning("sentiment fetch failed for %s: %s", pair, exc)
            sentiment = None

    if sentiment is None or sentiment.empty:
        for col, default in _SENTIMENT_NEUTRAL.items():
            dataframe[col] = default
        return dataframe

    df_sorted = _normalize_dt_column(dataframe.sort_values("date").reset_index(drop=True))
    sentiment_sorted = _normalize_dt_index(sentiment.sort_index())
    merged = pd.merge_asof(
        df_sorted, sentiment_sorted,
        left_on="date", right_index=True,
        direction="backward",
    )
    for col, default in _SENTIMENT_NEUTRAL.items():
        if col not in merged.columns:
            merged[col] = default
        else:
            merged[col] = merged[col].fillna(default)
    return merged


def _attach_regime(dataframe: DataFrame, pair: str) -> DataFrame:
    """
    Merge regime features (1h cadence) and the non-feature `regime_label` /
    `regime_confidence` columns used by gating logic.
    """
    regime = None
    if _REGIME_AVAILABLE and get_regime_features is not None:
        try:
            regime = get_regime_features(pair)
        except Exception as exc:
            logger.warning("regime fetch failed for %s: %s", pair, exc)
            regime = None

    if regime is None or regime.empty:
        for col, default in _REGIME_NEUTRAL.items():
            dataframe[col] = default
        dataframe["regime_label"] = "unknown"
        dataframe["regime_confidence"] = 0.0
        return dataframe

    df_sorted = _normalize_dt_column(dataframe.sort_values("date").reset_index(drop=True))
    regime_sorted = _normalize_dt_index(regime.sort_index())
    merged = pd.merge_asof(
        df_sorted, regime_sorted,
        left_on="date", right_index=True,
        direction="backward",
    )
    for col, default in _REGIME_NEUTRAL.items():
        if col not in merged.columns:
            merged[col] = default
        else:
            merged[col] = merged[col].fillna(default)
    if "regime_label" not in merged.columns:
        merged["regime_label"] = "unknown"
    else:
        merged["regime_label"] = merged["regime_label"].fillna("unknown")
    if "regime_confidence" not in merged.columns:
        merged["regime_confidence"] = 0.0
    else:
        merged["regime_confidence"] = merged["regime_confidence"].fillna(0.0)
    return merged


class FreqAIMeanRevV1(IStrategy, MonitoringMixin):
    INTERFACE_VERSION = 3

    minimal_roi = {
        "0": 0.04,
        "30": 0.02,
        "60": 0.01,
        "120": 0,
    }
    stoploss = -0.05
    trailing_stop = False
    use_custom_stoploss = True
    timeframe = "5m"
    can_short = False
    use_exit_signal = True
    exit_profit_only = False
    process_only_new_candles = True
    startup_candle_count: int = 80

    entry_threshold = DecimalParameter(0.55, 0.85, default=0.62, space="buy", optimize=True)
    exit_threshold = DecimalParameter(0.45, 0.75, default=0.55, space="sell", optimize=True)

    # Defaults — overridable via config.json[regime_gating].* (operator-tunable
    # without redeploy) or FREQTRADE__REGIME_GATING__<KEY> env vars.
    _DEFAULT_REGIME_GATING = {
        "entry_delta": {
            "trending_up": -0.05,
            "mean_reverting": 0.00,
            "high_volatility": 0.15,
            "trending_down": 0.20,    # was None — now: require base+0.20 floor
            "unknown": 0.00,
        },
        "exit_delta": {
            "mean_reverting": -0.10,
            "trending_up": 0.05,
            "high_volatility": 0.00,
            "trending_down": -0.20,
            "unknown": 0.00,
        },
        "high_vol_stake_factor": 0.5,
        "high_vol_min_confidence": 0.75,
        "mean_rev_take_profit": 0.015,
        "trending_up_trail_trigger": 0.03,
        "trending_up_trail_distance": -0.025,
        "tft_min_confidence": 0.40,
        "meta_min_confidence": 0.40,
    }

    @property
    def _regime_gating(self) -> dict:
        """config.json[regime_gating] merged on top of _DEFAULT_REGIME_GATING."""
        cfg = dict(self._DEFAULT_REGIME_GATING)
        override = (self.config.get("regime_gating", {}) or {})
        for k, v in override.items():
            if k.startswith("_"):     # skip _doc strings
                continue
            cfg[k] = v
        return cfg

    @property
    def REGIME_ENTRY_DELTA(self) -> dict:
        return self._regime_gating["entry_delta"]

    @property
    def REGIME_EXIT_DELTA(self) -> dict:
        return self._regime_gating["exit_delta"]

    @property
    def HIGH_VOL_STAKE_FACTOR(self) -> float:
        return float(self._regime_gating["high_vol_stake_factor"])

    @property
    def HIGH_VOL_MIN_CONFIDENCE(self) -> float:
        return float(self._regime_gating["high_vol_min_confidence"])

    @property
    def MEAN_REV_TAKE_PROFIT(self) -> float:
        return float(self._regime_gating["mean_rev_take_profit"])

    @property
    def TRENDING_UP_TRAIL_TRIGGER(self) -> float:
        return float(self._regime_gating["trending_up_trail_trigger"])

    @property
    def TRENDING_UP_TRAIL_DISTANCE(self) -> float:
        return float(self._regime_gating["trending_up_trail_distance"])

    @property
    def TFT_MIN_CONFIDENCE(self) -> float:
        return float(self._regime_gating["tft_min_confidence"])

    @property
    def META_MIN_CONFIDENCE(self) -> float:
        return float(self._regime_gating["meta_min_confidence"])
    # Cache loaded ensembles per save_dir so we don't re-deserialize each candle.
    _DRL_CACHE: "dict[str, object]" = {}

    # Risk governor instance — populated in bot_start. Gates every entry.
    _risk_governor: object | None = None
    # Monitoring state (_slack / _journal / _metrics / _recorded_closed_trades /
    # _journal_id_by_trade / _last_daily_summary_date / _risk_alert_state /
    # _last_metric_hour) is owned by MonitoringMixin and initialised in
    # _init_monitoring().

    # ── Capital allocation (config.json[capital_allocation]) ──
    # Drives per-pair max stake (pair_weights) and a rolling-Sharpe gate
    # (min_sharpe_for_trading) so weak pairs become data-only.
    _capital_allocation: dict = {}
    # Per-pair rolling Sharpe (annualised, last 14 days) cache; refreshed
    # at most once per hour from trade_journal.
    _pair_rolling_sharpe: dict = {}
    _rolling_sharpe_refreshed_at: float = 0.0
    # Compounded-equity log latch — emit at most one INFO line per UTC day.
    _last_compounding_log_date: str | None = None

    # ------------------------------------------------------------------
    # FreqAI feature engineering
    # ------------------------------------------------------------------

    def feature_engineering_expand_all(
        self, dataframe: DataFrame, period: int, metadata: Dict, **kwargs
    ) -> DataFrame:
        """Features expanded across `indicator_periods_candles` and timeframes."""
        dataframe[f"%-rsi-period_{period}"] = ta.RSI(dataframe, timeperiod=period)
        dataframe[f"%-atr-period_{period}"] = ta.ATR(dataframe, timeperiod=period)

        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=period, stds=2.2
        )
        dataframe[f"%-bb_width-period_{period}"] = (
            (bollinger["upper"] - bollinger["lower"]) / bollinger["mid"]
        )
        dataframe[f"%-bb_pct-period_{period}"] = (
            (dataframe["close"] - bollinger["lower"])
            / (bollinger["upper"] - bollinger["lower"])
        )

        dataframe[f"%-volume_sma_ratio-period_{period}"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        )

        # On-chain, sentiment and regime features are not period-dependent —
        # attach once and let subsequent period calls reuse them.
        if "%-onchain_mvrv" not in dataframe.columns:
            dataframe = _attach_onchain(dataframe, metadata.get("pair", ""))
        if "%-sentiment_score" not in dataframe.columns:
            dataframe = _attach_sentiment(dataframe, metadata.get("pair", ""))
        if "regime_label" not in dataframe.columns:
            dataframe = _attach_regime(dataframe, metadata.get("pair", ""))

        return dataframe

    def feature_engineering_expand_basic(
        self, dataframe: DataFrame, metadata: Dict, **kwargs
    ) -> DataFrame:
        """Features expanded only across timeframes."""
        macd = ta.MACD(dataframe)
        dataframe["%-macd"] = macd["macd"]
        dataframe["%-macdsignal"] = macd["macdsignal"]
        dataframe["%-macdhist"] = macd["macdhist"]
        dataframe["%-pct_change"] = dataframe["close"].pct_change()
        dataframe["%-raw_volume"] = dataframe["volume"]
        dataframe["%-raw_price"] = dataframe["close"]
        return dataframe

    def feature_engineering_standard(
        self, dataframe: DataFrame, metadata: Dict, **kwargs
    ) -> DataFrame:
        """Standard (non-expanded) features."""
        dataframe["%-day_of_week"] = (dataframe["date"].dt.dayofweek + 1) / 7
        dataframe["%-hour_of_day"] = (dataframe["date"].dt.hour + 1) / 25
        return dataframe

    def set_freqai_targets(
        self, dataframe: DataFrame, metadata: Dict, **kwargs
    ) -> DataFrame:
        """
        Three-class direction target over the next `label_period_candles`:
        up / flat / down, with `flat` defined by a deadband of
        `feature_parameters.flat_threshold_bps` basis points (default 10 bps
        = 0.10%). Set the threshold to 0 to fall back to binary up/down.
        """
        feature_params = self.freqai_info["feature_parameters"]
        flat_bps = float(feature_params.get("flat_threshold_bps", 10))
        flat_thresh = flat_bps / 10_000.0

        try:
            self.freqai.class_names = (
                ["down", "flat", "up"] if flat_thresh > 0 else ["down", "up"]
            )
        except Exception:
            pass

        period = feature_params["label_period_candles"]
        future_close = dataframe["close"].shift(-period)
        ret = (future_close - dataframe["close"]) / dataframe["close"]
        if flat_thresh > 0:
            dataframe["&-target"] = np.where(
                ret > flat_thresh, "up",
                np.where(ret < -flat_thresh, "down", "flat"),
            )
        else:
            dataframe["&-target"] = np.where(ret > 0, "up", "down")
        return dataframe

    # ------------------------------------------------------------------
    # Strategy hooks
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Risk governor lifecycle
    # ------------------------------------------------------------------

    def bot_start(self, **kwargs) -> None:
        """Instantiate risk governor + monitoring once at bot startup."""
        if _RISK_AVAILABLE and RiskGovernor is not None:
            try:
                self._risk_governor = RiskGovernor.from_config(self.config)
                logger.info("[strategy] risk governor wired in")
            except Exception as exc:
                logger.warning("[strategy] failed to init risk governor: %s", exc)
                self._risk_governor = None
        else:
            self._risk_governor = None

        # MonitoringMixin owns Slack/journal/metrics setup. Safe no-op if
        # any of the optional monitoring modules failed to import.
        self._init_monitoring(self.config)

        # Capital-allocation block (config.json[capital_allocation]). Safe
        # default: empty dict → all pair_weights default to 1.0 (no cap),
        # min_sharpe_for_trading defaults to 0.0 (gate disabled).
        self._capital_allocation = dict(self.config.get("capital_allocation") or {})
        if self._capital_allocation:
            mode = self._capital_allocation.get("mode", "performance_weighted")
            n_weights = len(self._capital_allocation.get("pair_weights") or {})
            logger.info(
                "[strategy] capital allocation: mode=%s, %d pair_weights, "
                "min_sharpe_for_trading=%.2f",
                mode, n_weights,
                float(self._capital_allocation.get("min_sharpe_for_trading", 0.0)),
            )

    def bot_loop_start(self, current_time, **kwargs) -> None:
        """
        Per-iteration tick: refresh equity, harvest newly-closed trades,
        emit hourly metrics snapshot + daily summary, fire risk alerts.
        """
        gov = self._risk_governor
        equity = 0.0
        try:
            equity = float(self.wallets.get_total_stake_amount())
            if gov is not None:
                gov.update_equity(equity)
        except Exception:
            pass

        # Risk-threshold alerts (warning at 5% DD, critical at 8%) — mixin
        # owns the latch state so we don't spam.
        self._send_risk_alert(gov)

        # Drain newly-closed trades. The mixin's _record_trade_exit is
        # idempotent per trade-id and also calls gov.record_trade_close so
        # risk + monitoring share the once-per-trade gate.
        try:
            from freqtrade.persistence import Trade
            closed = Trade.get_trades_proxy(is_open=False)
        except Exception:
            closed = []
        for t in closed:
            self._record_trade_exit(t, gov=gov)

        # Hourly snapshot + daily summary — both gated internally so it's
        # safe to call every iteration.
        self._maybe_write_hourly_snapshot(current_time, equity, gov)
        self._maybe_send_daily_summary(current_time, gov)

        # Refresh per-pair 14-day rolling Sharpe at most once per hour, used
        # by the capital_allocation gate (min_sharpe_for_trading) and by the
        # entry-threshold tweak in populate_entry_trend.
        self._refresh_rolling_sharpe_if_due(current_time)

        # Once-per-UTC-day compounded-equity INFO line (visibility only —
        # freqtrade already compounds in dry-run via its in-memory wallet).
        self._maybe_log_compounding(current_time)

    # ------------------------------------------------------------------
    # Capital allocation + rolling-Sharpe gate
    # ------------------------------------------------------------------

    def _pair_weight(self, pair: str) -> float:
        """Max fraction of equity allowed for this pair (0 = data-only).

        Comes from config.json[capital_allocation][pair_weights]. Defaults to
        1.0 if no allocation is configured (i.e. no per-pair cap).
        """
        if not self._capital_allocation:
            return 1.0
        weights = self._capital_allocation.get("pair_weights") or {}
        if pair not in weights:
            return 0.0  # missing = excluded
        try:
            return max(0.0, min(1.0, float(weights[pair])))
        except (TypeError, ValueError):
            return 0.0

    def _min_sharpe_for_trading(self) -> float:
        if not self._capital_allocation:
            return 0.0
        return float(self._capital_allocation.get("min_sharpe_for_trading", 0.0))

    def _refresh_rolling_sharpe_if_due(self, now=None) -> None:
        """Re-compute per-pair 14-day rolling Sharpe at most once an hour.

        Reads ``trade_journal``, groups by pair, computes annualised Sharpe
        on daily P&L percentages. Pairs with no trades stay at None — the
        gate treats None as "no data → allow" (let the system bootstrap).

        Side benefit: also re-reads config.json[capital_allocation] from
        disk so an external rebalance (scripts/rebalance_capital.py) is
        picked up within an hour without a freqtrade restart.
        """
        import time as _time
        now_ts = _time.time()
        if (now_ts - self._rolling_sharpe_refreshed_at) < 3600:
            return
        self._rolling_sharpe_refreshed_at = now_ts

        # Re-read capital_allocation from disk (auto-rebalance support)
        try:
            import json as _json
            cfg_path = "/freqtrade/user_data/config.json"
            with open(cfg_path) as f:
                fresh = _json.load(f).get("capital_allocation")
            if fresh:
                old_weights = (self._capital_allocation or {}).get("pair_weights") or {}
                new_weights = fresh.get("pair_weights") or {}
                if old_weights != new_weights:
                    logger.info(
                        "[strategy] capital_allocation reloaded from disk: %s → %s",
                        old_weights, new_weights,
                    )
                self._capital_allocation = fresh
        except Exception as exc:
            logger.debug("[strategy] capital_allocation reload failed: %s", exc)

        try:
            sys.path.insert(0, str(_USER_DATA))
            from modules import db as _db
            with _db.cursor() as cur:
                cur.execute(
                    """
                    SELECT pair, closed_at, pnl_pct
                    FROM trade_journal
                    WHERE closed_at IS NOT NULL
                      AND closed_at > NOW() - INTERVAL '14 days'
                    ORDER BY pair, closed_at
                    """
                )
                rows = cur.fetchall()
        except Exception as exc:
            logger.debug("[strategy] rolling Sharpe refresh failed: %s", exc)
            return

        # Group: pair → list of daily-P&L-pct sums
        from collections import defaultdict
        import math as _math
        per_pair_daily: dict[str, dict[str, float]] = defaultdict(dict)
        for r in rows:
            pair = r["pair"]
            day = r["closed_at"].strftime("%Y-%m-%d")
            per_pair_daily[pair][day] = (
                per_pair_daily[pair].get(day, 0.0) + float(r["pnl_pct"] or 0.0)
            )

        new_cache: dict[str, float] = {}
        for pair, daily in per_pair_daily.items():
            pcts = list(daily.values())
            if len(pcts) < 2:
                continue
            mean = sum(pcts) / len(pcts)
            var = sum((x - mean) ** 2 for x in pcts) / (len(pcts) - 1)
            sd = _math.sqrt(var)
            if sd <= 0:
                continue
            sharpe = (mean / sd) * _math.sqrt(365)
            new_cache[pair] = float(sharpe)
        self._pair_rolling_sharpe = new_cache
        if new_cache:
            logger.info(
                "[strategy] rolling 14d Sharpe refreshed: %s",
                {p: round(s, 2) for p, s in sorted(new_cache.items())},
            )

    def _entry_threshold_adjust(self, pair: str, base: float) -> float:
        """Per-pair entry-threshold tweak based on rolling Sharpe.

        Stronger pairs (live Sharpe > 0.9) get a -0.05 nudge so the bot
        enters more easily; weaker pairs (< 0.7) get +0.10. Pairs with no
        live data are unchanged. Below ``min_sharpe_for_trading`` the gate
        in populate_entry_trend blocks entry outright, so this method's
        +0.10 branch is a defence-in-depth layer.
        """
        s = self._pair_rolling_sharpe.get(pair)
        if s is None:
            return base
        if s > 0.9:
            return base - 0.05
        if s < 0.7:
            return base + 0.10
        return base

    def _maybe_log_compounding(self, now) -> None:
        """Once per UTC day, INFO-log the compounded paper-trading equity.

        Freqtrade already compounds in dry-run by tracking the wallet in
        memory (initial dry_run_wallet + accumulated P&L). This hook only
        surfaces the number for operator visibility; it does NOT mutate
        config.json, which would require a restart to take effect.
        """
        try:
            today = now.strftime("%Y-%m-%d")
        except Exception:
            return
        if self._last_compounding_log_date == today:
            return
        # Run shortly after midnight UTC so the previous day's last close
        # has settled in the wallet.
        if not (0 <= getattr(now, "hour", 0) <= 1):
            return
        try:
            initial = float(self.config.get("dry_run_wallet", 0) or 0)
            current_equity = float(self.wallets.get_total_stake_amount())
            cum_pnl = current_equity - initial
            growth_pct = (cum_pnl / initial * 100.0) if initial else 0.0
            logger.info(
                "[compounding] day=%s initial_wallet=$%.2f current_equity=$%.2f "
                "cumulative_pnl=$%+.2f growth=%+.2f%%",
                today, initial, current_equity, cum_pnl, growth_pct,
            )
            self._last_compounding_log_date = today
        except Exception as exc:
            logger.debug("[compounding] log failed: %s", exc)

    def _open_positions_snapshot(self) -> list[tuple[str, float]]:
        """Return [(pair, current_stake_in_quote), ...] for all open trades."""
        try:
            from freqtrade.persistence import Trade
            return [
                (str(t.pair), float(t.stake_amount or 0.0))
                for t in Trade.get_trades_proxy(is_open=True)
            ]
        except Exception:
            return []

    def _pair_returns_for_correlation(self, pairs: list[str]) -> dict[str, "pd.Series"]:
        """
        Build a {pair: returns Series} dict for the governor's correlation gate.
        Uses each pair's analyzed dataframe (close column → pct_change).
        """
        out: dict[str, pd.Series] = {}
        for pair in pairs:
            try:
                df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            except Exception:
                continue
            if df is None or df.empty or "close" not in df.columns or "date" not in df.columns:
                continue
            s = df.set_index("date")["close"].pct_change().dropna()
            if not s.empty:
                out[pair] = s
        return out

    def confirm_trade_entry(
        self, pair: str, order_type: str, amount: float, rate: float,
        time_in_force: str, current_time, entry_tag, side: str, **kwargs,
    ) -> bool:
        """Final risk gate. Return False to abort the order."""
        gov = self._risk_governor
        if gov is None:
            return True

        equity = 0.0
        try:
            equity = float(self.wallets.get_total_stake_amount())
        except Exception:
            pass

        # Stake at this rate, in quote currency
        proposed_stake_quote = float(amount) * float(rate)

        # Gather meta-agent confidence for Kelly
        meta_conf: float | None = None
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and not df.empty and "meta_confidence" in df.columns:
                meta_conf = float(df.iloc[-1].get("meta_confidence", 0.0) or 0.0)
        except Exception:
            pass

        open_positions = self._open_positions_snapshot()
        peer_pairs = [p for p, _ in open_positions if p != pair]
        pair_returns = self._pair_returns_for_correlation([pair, *peer_pairs])

        decision = gov.approve_entry(
            pair=pair,
            signal_price=float(rate),
            base_stake=proposed_stake_quote,
            equity=equity,
            model_confidence=meta_conf,
            open_positions=open_positions,
            pair_returns=pair_returns,
        )
        if not decision.approved:
            logger.warning(
                "[strategy] risk-blocked %s: %s (constraint=%s)",
                pair, decision.reason, decision.blocking_constraint,
            )
            return False

        # Approval — hand the context off to the monitoring mixin which
        # fires Slack + journal log_entry + Influx regime/sentiment writes.
        latest = self._latest_signals_for(pair)
        self._record_trade_entry(
            pair=pair,
            side=str(side or "long"),
            rate=float(rate),
            stake=proposed_stake_quote,
            confidence=meta_conf,
            latest=latest,
            entry_tag=entry_tag,
        )
        return True

    def _latest_signals_for(self, pair: str) -> dict:
        """Pull the latest signal context (TFT probs, DRL votes, regime, sentiment) for a pair."""
        out: dict = {
            "tft_probs": None, "drl_votes": None, "regime": None,
            "sentiment_score": None, "sentiment_confidence": None,
            "features_used": None, "reasoning": None,
        }
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        except Exception:
            return out
        if df is None or df.empty:
            return out
        last = df.iloc[-1]
        try:
            out["tft_probs"] = {
                "down": float(last.get("down", 0.0)),
                "flat": float(last.get("flat", 0.0)),
                "up": float(last.get("up", 0.0)),
            }
        except Exception:
            pass
        for k in ("regime_label", "%-sentiment_score", "%-sentiment_confidence"):
            try:
                v = last.get(k, None)
                if v is None:
                    continue
                if k == "regime_label":
                    out["regime"] = str(v)
                elif k == "%-sentiment_score":
                    out["sentiment_score"] = float(v)
                else:
                    out["sentiment_confidence"] = float(v)
            except Exception:
                pass
        # Feature columns the FreqAI pipeline consumed for this row
        feat_cols = [c for c in df.columns if c.startswith("%-")]
        out["features_used"] = feat_cols[:60]   # cap to avoid bloating the journal
        # Reasoning string — short summary
        meta_sig = int(last.get("meta_signal", 0) or 0)
        meta_conf = float(last.get("meta_confidence", 0.0) or 0.0)
        out["reasoning"] = (
            f"meta_signal={meta_sig:+d} conf={meta_conf:.2f} regime={out['regime']} "
            f"tft_up={(out['tft_probs'] or {}).get('up', 0):.2f}"
        )
        return out

    # ------------------------------------------------------------------
    # FreqAI populate
    # ------------------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        # Belt-and-braces: ensure gating columns survive the FreqAI pipeline
        if "regime_label" not in dataframe.columns:
            dataframe = _attach_regime(dataframe, metadata.get("pair", ""))
        # Compute meta-agent (TFT + DRL ensemble) signal columns. No-op
        # when the DRL weights aren't loadable yet (cold start).
        dataframe = self._compute_meta_signals(dataframe)
        # Pydantic v2 (freqtrade 2026.4 / py3.14) refuses to serialize
        # numpy.float32 — it is not a subclass of Python float the way
        # numpy.float64 is, so /api/v1/pair_candles 500s on every poll.
        # TFTModel emits up/down/flat/tft_confidence as float32; when
        # FreqAI mixes those with NaN backfill the column dtype lands as
        # `object` with float32 cells, which `select_dtypes(['float32'])`
        # misses. Cast both float32 columns and the named FreqAI
        # prediction columns to float64 so the API serializer accepts
        # them.
        f32_cols = list(dataframe.select_dtypes(include=["float32"]).columns)
        for col in ("up", "down", "flat", "tft_confidence"):
            if col in dataframe.columns and col not in f32_cols:
                if dataframe[col].dtype != "float64":
                    f32_cols.append(col)
        for col in f32_cols:
            dataframe[col] = pd.to_numeric(dataframe[col], errors="coerce").astype("float64")
        return dataframe

    # ------------------------------------------------------------------
    # Meta-agent integration
    # ------------------------------------------------------------------

    def _load_drl_ensemble(self):
        """Lazy, cached load of the DRL ensemble. Returns None on miss."""
        if not _DRL_AVAILABLE or DRLEnsemble is None:
            return None
        save_dir = str(DRL_SAVE_DIR)
        cached = self._DRL_CACHE.get(save_dir)
        if cached is not None:
            return cached
        try:
            ensemble = DRLEnsemble(save_dir=DRL_SAVE_DIR, device="cpu")
            ensemble.load()
        except FileNotFoundError:
            self._DRL_CACHE[save_dir] = None
            return None
        except Exception as exc:
            logger.warning("DRL ensemble failed to load (%s); falling back to TFT", exc)
            self._DRL_CACHE[save_dir] = None
            return None
        self._DRL_CACHE[save_dir] = ensemble
        return ensemble

    def _build_observation_matrix(self, df: DataFrame) -> "np.ndarray | None":
        """Vectorised observation construction matching trading_env.TradingEnv."""
        required = ["close", "up", "down"]
        for col in required:
            if col not in df.columns:
                return None
        n = len(df)
        obs = np.zeros((n, 17), dtype=np.float32)
        # 0..2 TFT — order in env is (down, flat, up)
        down = df["down"].astype(np.float32).fillna(0.0).to_numpy()
        flat = (df["flat"].astype(np.float32).fillna(0.0).to_numpy()
                if "flat" in df.columns else np.zeros(n, dtype=np.float32))
        up = df["up"].astype(np.float32).fillna(0.0).to_numpy()
        rowsum = down + flat + up
        rowsum[rowsum < 1e-6] = 1.0
        obs[:, 0] = down / rowsum
        obs[:, 1] = flat / rowsum
        obs[:, 2] = up / rowsum
        # 3..6 onchain (zero-padded if a source column missing)
        oc_cols = list(_ONCHAIN_NEUTRAL.keys())
        for i, col in enumerate(oc_cols[:4]):
            if col in df.columns:
                obs[:, 3 + i] = df[col].astype(np.float32).fillna(_ONCHAIN_NEUTRAL[col]).to_numpy()
        # 7 derived onchain pressure
        obs[:, 7] = obs[:, 3] * (obs[:, 4] - 1.0)  # netflow_z * (mvrv - 1)
        # 8..9 sentiment
        if "%-sentiment_score" in df.columns:
            obs[:, 8] = df["%-sentiment_score"].astype(np.float32).fillna(0.0).to_numpy()
        if "%-sentiment_confidence" in df.columns:
            obs[:, 9] = df["%-sentiment_confidence"].astype(np.float32).fillna(0.0).to_numpy()
        # 10..13 regime one-hot
        for i, label in enumerate(REGIME_LABELS):
            col = f"%-regime_is_{label}"
            if col in df.columns:
                obs[:, 10 + i] = df[col].astype(np.float32).fillna(0.0).to_numpy()
            elif "regime_label" in df.columns:
                obs[:, 10 + i] = (df["regime_label"].astype(str) == label).astype(np.float32).to_numpy()
        # 14..16 portfolio state — unknown when scoring offline; leave zero
        # (cash_ratio=0, position=0, unrealized=0). The agents are trained on
        # full episodes where this *is* observable; for live inference this is
        # an approximation.  Better: feed the actual broker state per call —
        # left as a follow-up since FreqAI's predict path doesn't expose it.
        return np.clip(obs, -5.0, 5.0)

    def _compute_meta_signals(self, dataframe: DataFrame) -> DataFrame:
        """Add `meta_signal`, `meta_confidence`, `meta_position_size` columns."""
        n = len(dataframe)
        # Defaults: zero so any AND-gating below is a no-op when the meta
        # column is missing (we treat 0 as "no opinion").
        dataframe["meta_signal"] = 0
        dataframe["meta_confidence"] = 0.0
        dataframe["meta_position_size"] = 0.0
        dataframe["meta_blocked_reason"] = ""

        if not _DRL_AVAILABLE or vote_batch is None or meta_compute_signal is None:
            return dataframe

        ensemble = self._load_drl_ensemble()
        if ensemble is None:
            return dataframe

        obs = self._build_observation_matrix(dataframe)
        if obs is None or n == 0:
            return dataframe

        try:
            actions = ensemble.predict(obs)
            votes = vote_batch(actions)
        except Exception as exc:
            logger.warning("DRL ensemble predict failed: %s", exc)
            return dataframe

        sig = np.zeros(n, dtype=np.int64)
        conf = np.zeros(n, dtype=np.float32)
        size = np.zeros(n, dtype=np.float32)
        reasons = [""] * n

        labels = (
            dataframe["regime_label"].astype(str).to_numpy()
            if "regime_label" in dataframe.columns else np.array(["unknown"] * n)
        )
        regime_conf = (
            dataframe["regime_confidence"].astype(float).fillna(0.0).to_numpy()
            if "regime_confidence" in dataframe.columns else np.ones(n)
        )
        tft_conf_arr = (
            dataframe["tft_confidence"].astype(float).fillna(0.0).to_numpy()
            if "tft_confidence" in dataframe.columns else np.ones(n)
        )
        down = dataframe["down"].astype(float).fillna(0.0).to_numpy()
        up = dataframe["up"].astype(float).fillna(0.0).to_numpy()
        flat = (dataframe["flat"].astype(float).fillna(0.0).to_numpy()
                if "flat" in dataframe.columns else 1.0 - down - up)

        for i, v in enumerate(votes):
            ms = meta_compute_signal(
                tft_probs={"down": float(down[i]), "flat": float(flat[i]), "up": float(up[i])},
                tft_confidence=float(tft_conf_arr[i]),
                drl_vote=v,
                regime=str(labels[i]),
                regime_confidence=float(regime_conf[i]),
                min_trade_confidence=self.META_MIN_CONFIDENCE,
            )
            sig[i] = ms.final_signal
            conf[i] = ms.final_confidence
            size[i] = ms.position_size_pct
            reasons[i] = ms.blocked_reason or ""

        dataframe["meta_signal"] = sig
        dataframe["meta_confidence"] = conf
        dataframe["meta_position_size"] = size
        dataframe["meta_blocked_reason"] = reasons
        return dataframe

    # ------------------------------------------------------------------
    # Regime-conditional entry / exit
    # ------------------------------------------------------------------

    def _per_row_threshold(
        self, dataframe: DataFrame, base: float, deltas: dict[str, float | None],
        sentinel_no_signal: float = 1.1,
    ) -> pd.Series:
        out = pd.Series(base, index=dataframe.index, dtype=float)
        if "regime_label" not in dataframe.columns:
            return out
        labels = dataframe["regime_label"].astype(str)
        for label, delta in deltas.items():
            mask = labels == label
            if not mask.any():
                continue
            if delta is None:
                out[mask] = sentinel_no_signal
            else:
                out[mask] = max(0.0, min(1.0, base + delta))
        return out

    def _meta_active(self, dataframe: DataFrame) -> bool:
        """True if the meta-agent populated non-zero signals on this dataframe."""
        if "meta_signal" not in dataframe.columns:
            return False
        return bool((dataframe["meta_signal"] != 0).any()
                    or (dataframe["meta_position_size"] > 0).any())

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata.get("pair", "")
        # Capital-allocation gates (no-op if config has no [capital_allocation]).
        # 1) Pair excluded entirely (weight=0): block all entries; model still
        #    trains so the data feed stays warm.
        # 2) Rolling 14d live Sharpe known and below the floor: block.
        if pair and self._capital_allocation:
            if self._pair_weight(pair) <= 0.0:
                dataframe["enter_long"] = 0
                return dataframe
            min_sharpe = self._min_sharpe_for_trading()
            live_sharpe = self._pair_rolling_sharpe.get(pair)
            if min_sharpe > 0 and live_sharpe is not None and live_sharpe < min_sharpe:
                dataframe["enter_long"] = 0
                return dataframe

        # Per-pair entry-threshold tweak: stronger live Sharpe lowers the bar,
        # weaker raises it. base is the strategy's hyperopt-tunable default.
        base = self._entry_threshold_adjust(pair, float(self.entry_threshold.value))
        threshold = self._per_row_threshold(dataframe, base, self.REGIME_ENTRY_DELTA)

        long_conditions = [
            dataframe["do_predict"] == 1,
            dataframe["up"] >= threshold,
            dataframe["volume"] > 0,
        ]
        # In trending_down: hard block long entries.
        if "regime_label" in dataframe.columns:
            long_conditions.append(dataframe["regime_label"] != "trending_down")
        # In high_volatility: require very high model confidence on top.
        if "regime_label" in dataframe.columns:
            long_conditions.append(
                (dataframe["regime_label"] != "high_volatility")
                | (dataframe["up"] >= self.HIGH_VOL_MIN_CONFIDENCE)
            )
        # TFT quantile-spread confidence — only enforced if the column is present.
        if "tft_confidence" in dataframe.columns:
            long_conditions.append(dataframe["tft_confidence"] >= self.TFT_MIN_CONFIDENCE)

        # Meta-agent gate: when the DRL ensemble is loaded, require
        # meta_signal == +1 AND meta_confidence ≥ threshold. We still keep
        # the TFT-based conditions above as a hard floor.
        meta_active = self._meta_active(dataframe)
        if meta_active:
            long_conditions.append(dataframe["meta_signal"] == 1)
            long_conditions.append(dataframe["meta_confidence"] >= self.META_MIN_CONFIDENCE)

        tag = "meta_up_regime" if meta_active else "freqai_up_regime"
        dataframe.loc[
            reduce(lambda a, b: a & b, long_conditions),
            ["enter_long", "enter_tag"],
        ] = (1, tag)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        base = float(self.exit_threshold.value)
        threshold = self._per_row_threshold(
            dataframe, base, self.REGIME_EXIT_DELTA, sentinel_no_signal=base,
        )

        exit_conditions = [
            dataframe["do_predict"] == 1,
            dataframe["down"] >= threshold,
        ]
        # When the meta-agent is active, exit on meta_signal == -1 in
        # addition to the legacy `down`-prob threshold, so we react to
        # consensus between TFT + DRL even on softer down-probabilities.
        if self._meta_active(dataframe):
            meta_exit = (dataframe["meta_signal"] == -1) & (
                dataframe["meta_confidence"] >= self.META_MIN_CONFIDENCE
            )
            exit_conditions = [
                dataframe["do_predict"] == 1,
                (dataframe["down"] >= threshold) | meta_exit,
            ]

        tag = "meta_down_regime" if self._meta_active(dataframe) else "freqai_down_regime"
        dataframe.loc[
            reduce(lambda a, b: a & b, exit_conditions),
            ["exit_long", "exit_tag"],
        ] = (1, tag)
        return dataframe

    # ------------------------------------------------------------------
    # Regime-aware sizing, trailing stop and take-profit
    # ------------------------------------------------------------------

    def _current_regime(self, pair: str) -> tuple[str, float]:
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        except Exception:
            return ("unknown", 0.0)
        if df is None or df.empty or "regime_label" not in df.columns:
            return ("unknown", 0.0)
        last = df.iloc[-1]
        return (
            str(last.get("regime_label", "unknown")),
            float(last.get("regime_confidence", 0.0) or 0.0),
        )

    def _last_meta_position_size(self, pair: str) -> float | None:
        """Latest meta-agent position_size_pct for this pair, or None if unknown."""
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        except Exception:
            return None
        if df is None or df.empty or "meta_position_size" not in df.columns:
            return None
        v = float(df.iloc[-1].get("meta_position_size", 0.0) or 0.0)
        return v if v > 0.0 else None

    def custom_stake_amount(
        self, pair: str, current_time, current_rate: float,
        proposed_stake: float, min_stake: float | None,
        max_stake: float, leverage: float, entry_tag: str | None,
        side: str, **kwargs,
    ) -> float:
        # Start from the meta-agent's recommended size when available.
        meta_size = self._last_meta_position_size(pair)
        stake = proposed_stake
        if meta_size is not None:
            stake = proposed_stake * meta_size

        # Stack the existing high-vol penalty on top so the conservative
        # floor still applies if the meta-agent is too generous.
        regime, _ = self._current_regime(pair)
        if regime == "high_volatility":
            stake = stake * self.HIGH_VOL_STAKE_FACTOR

        # Risk governor: apply max_position_pct cap and Kelly suggestion.
        gov = self._risk_governor
        if gov is not None:
            try:
                equity = float(self.wallets.get_total_stake_amount())
            except Exception:
                equity = 0.0
            meta_conf: float | None = None
            try:
                df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
                if df is not None and not df.empty and "meta_confidence" in df.columns:
                    meta_conf = float(df.iloc[-1].get("meta_confidence", 0.0) or 0.0)
            except Exception:
                pass
            try:
                decision = gov.approve_entry(
                    pair=pair,
                    signal_price=float(current_rate),
                    base_stake=float(stake),
                    equity=equity,
                    model_confidence=meta_conf,
                    open_positions=self._open_positions_snapshot(),
                    pair_returns=None,    # correlation re-checked in confirm_trade_entry
                )
                if decision.approved and decision.suggested_stake > 0:
                    stake = float(decision.suggested_stake)
            except Exception as exc:
                logger.debug("[strategy] governor sizing call failed: %s", exc)

        # Capital-allocation cap: stake ≤ pair_weight × equity. Ensures
        # ETH/BTC concentration matches config without touching the
        # max_position_size_pct floor in risk_management.
        weight = self._pair_weight(pair)
        if weight > 0.0 and weight < 1.0:
            try:
                equity_for_cap = float(self.wallets.get_total_stake_amount())
                pair_cap = equity_for_cap * weight
                if stake > pair_cap > 0:
                    logger.debug(
                        "[strategy] %s: capping stake %.2f → %.2f (weight=%.2f, equity=%.2f)",
                        pair, stake, pair_cap, weight, equity_for_cap,
                    )
                    stake = pair_cap
            except Exception:
                pass

        if min_stake is not None:
            stake = max(stake, min_stake)
        return min(stake, max_stake)

    def custom_stoploss(
        self, pair: str, trade, current_time, current_rate: float,
        current_profit: float, after_fill: bool = False, **kwargs,
    ) -> float:
        regime, _ = self._current_regime(pair)
        # In trending_up: trail wider once meaningfully in profit.
        if regime == "trending_up" and current_profit > self.TRENDING_UP_TRAIL_TRIGGER:
            return self.TRENDING_UP_TRAIL_DISTANCE
        return self.stoploss

    def custom_exit(
        self, pair: str, trade, current_time, current_rate: float,
        current_profit: float, **kwargs,
    ) -> str | None:
        regime, _ = self._current_regime(pair)
        # In mean_reverting: take quick profits at +1.5%.
        if regime == "mean_reverting" and current_profit >= self.MEAN_REV_TAKE_PROFIT:
            return "regime_mean_rev_tp"
        return None
