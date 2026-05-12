"""Candle sources for the backtest engine.

A :class:`CandleSource` yields :class:`quanta_core.types.Bar` objects in
strictly chronological order for one ``(symbol, timeframe)`` pair. Two
implementations land in wave 2:

* :class:`FeatherCandleSource` — reads the legacy
  ``user_data/data/coinbase/<symbol>-<tf>.feather`` (or matching ``.parquet``)
  layout. The legacy artefacts are treated as read-only: this loader never
  rewrites them.
* :class:`SyntheticCandleSource` — deterministic random walk for tests.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from quanta_core.types import Bar, Symbol, Timeframe

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable, Iterator, Sequence

    import pandas as pd


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CandleSourceError(RuntimeError):
    """Raised when a candle source cannot satisfy a read."""


# ---------------------------------------------------------------------------
# Timeframe → timedelta helper (also used by the engine clock).
# ---------------------------------------------------------------------------

_TIMEFRAME_TO_TIMEDELTA: dict[Timeframe, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


def timeframe_to_timedelta(tf: Timeframe) -> timedelta:
    """Return the canonical :class:`datetime.timedelta` for one timeframe."""
    return _TIMEFRAME_TO_TIMEDELTA[tf]


# ---------------------------------------------------------------------------
# Abstract source
# ---------------------------------------------------------------------------


class CandleSource(ABC):
    """Abstract base for chronological :class:`Bar` iteration."""

    symbol: Symbol
    timeframe: Timeframe

    @abstractmethod
    def __iter__(self) -> Iterator[Bar]:
        """Yield :class:`Bar` objects in chronological order."""

    def slice(self, start: datetime, end: datetime) -> Iterator[Bar]:
        """Yield only the bars whose ``timestamp_utc`` falls in ``[start, end)``.

        Both bounds must be timezone-aware. Implementations may load the full
        store lazily; the default implementation filters the full iterator.
        """
        _require_utc("start", start)
        _require_utc("end", end)
        if end <= start:
            msg = f"end ({end}) must be strictly after start ({start})"
            raise CandleSourceError(msg)
        for bar in self:
            ts = bar.timestamp_utc
            if ts < start:
                continue
            if ts >= end:
                return
            yield bar


# ---------------------------------------------------------------------------
# Feather / parquet source — wraps the existing legacy on-disk layout.
# ---------------------------------------------------------------------------


class FeatherCandleSource(CandleSource):
    """Read-only OHLCV loader for ``user_data/data/<venue>/`` layouts.

    The legacy layout writes one ``<SYMBOL>-<TF>.feather`` (and sibling
    ``.parquet``) per pair / timeframe. We pick the first extant file in the
    extension priority order ``.feather`` -> ``.parquet`` to keep this
    backwards-compatible with both eras of the trading-bot history.

    The loader never writes to disk; the wave-2 task statement is explicit
    that ``user_data/data/coinbase/`` is read-only. Tests should use the
    synthetic source.
    """

    EXPECTED_COLUMNS: tuple[str, ...] = ("date", "open", "high", "low", "close", "volume")

    def __init__(
        self,
        *,
        symbol: Symbol,
        timeframe: Timeframe,
        root: Path,
    ) -> None:
        """Construct a reader rooted at ``root`` (the venue's data directory)."""
        self.symbol = symbol
        self.timeframe = timeframe
        self.root = Path(root)
        self._path = self._resolve_path()

    # ------------------------------------------------------------------
    # Path resolution + load
    # ------------------------------------------------------------------

    def _resolve_path(self) -> Path:
        """Pick the on-disk file for ``(symbol, timeframe)``."""
        if not self.root.is_dir():
            msg = f"candle root does not exist or is not a directory: {self.root}"
            raise CandleSourceError(msg)
        # Two filename conventions exist in the wild: "BTC_USD-5m" and
        # "BTC-USD-5m". Try both, alongside the bare "BTC/USD" pass-through.
        sym_variants = [
            self.symbol,
            self.symbol.replace("/", "_"),
            self.symbol.replace("/", "-"),
            self.symbol.replace("/", ""),
        ]
        for sym in sym_variants:
            for ext in (".feather", ".parquet"):
                cand = self.root / f"{sym}-{self.timeframe}{ext}"
                if cand.is_file():
                    return cand
        msg = (
            f"no OHLCV file found for symbol={self.symbol} tf={self.timeframe} "
            f"under {self.root} (tried .feather and .parquet)"
        )
        raise CandleSourceError(msg)

    def _load(self) -> pd.DataFrame:  # type: ignore[no-any-unimported]
        """Load the resolved file into a pandas DataFrame, lazily."""
        # Local import keeps the import-time cost of the module small and lets
        # the SyntheticCandleSource path run on systems without pyarrow.
        import pandas as pd

        path = self._path
        if path.suffix == ".feather":
            df = pd.read_feather(path)
        elif path.suffix == ".parquet":
            df = pd.read_parquet(path)
        else:  # pragma: no cover — _resolve_path filters extensions
            msg = f"unsupported file extension: {path.suffix}"
            raise CandleSourceError(msg)
        missing = [c for c in self.EXPECTED_COLUMNS if c not in df.columns]
        if missing:
            msg = f"OHLCV file {path} is missing columns: {missing}"
            raise CandleSourceError(msg)
        # Sort ascending and drop any duplicate timestamps deterministically.
        df = (
            df.sort_values("date")
            .drop_duplicates(subset=["date"], keep="last")
            .reset_index(drop=True)
        )
        return df

    # ------------------------------------------------------------------
    # Iteration
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[Bar]:
        """Yield :class:`Bar` objects in chronological order."""
        df = self._load()
        symbol = self.symbol
        timeframe = self.timeframe
        for row in df.itertuples(index=False):
            ts = _coerce_utc(row.date)
            yield Bar(
                symbol=symbol,
                open=Decimal(str(row.open)),
                high=Decimal(str(row.high)),
                low=Decimal(str(row.low)),
                close=Decimal(str(row.close)),
                volume=Decimal(str(row.volume)),
                timestamp_utc=ts,
                timeframe=timeframe,
            )


# ---------------------------------------------------------------------------
# Synthetic source — deterministic random walk for tests.
# ---------------------------------------------------------------------------


class SyntheticCandleSource(CandleSource):
    """Deterministic OHLCV generator for unit + parity tests.

    Generates ``n_bars`` bars from a seeded random walk centred on
    ``start_price``. The walk is reproducible: the same ``(seed, n_bars,
    start_price)`` always emits the same sequence.
    """

    def __init__(
        self,
        *,
        symbol: Symbol,
        timeframe: Timeframe,
        start: datetime,
        n_bars: int,
        seed: int = 0,
        start_price: Decimal = Decimal("100"),
        volatility: Decimal = Decimal("1"),
        drift: Decimal = Decimal("0"),
    ) -> None:
        """Configure the synthetic walk."""
        _require_utc("start", start)
        if n_bars <= 0:
            msg = f"n_bars must be positive, got {n_bars}"
            raise ValueError(msg)
        if volatility < 0:
            msg = f"volatility must be non-negative, got {volatility}"
            raise ValueError(msg)
        self.symbol = symbol
        self.timeframe = timeframe
        self.start = start
        self.n_bars = n_bars
        self.seed = seed
        self.start_price = Decimal(start_price)
        self.volatility = Decimal(volatility)
        self.drift = Decimal(drift)

    def __iter__(self) -> Iterator[Bar]:
        """Yield ``n_bars`` deterministic bars from the seeded walk."""
        rng = random.Random(self.seed)
        step = timeframe_to_timedelta(self.timeframe)
        prev_close = self.start_price
        ts = self.start
        for _ in range(self.n_bars):
            # Symmetric random shock in [-volatility, +volatility]
            shock = Decimal(str(rng.uniform(-1.0, 1.0))) * self.volatility
            new_close = max(Decimal("0.01"), prev_close + self.drift + shock)
            # Build OHLC respecting low ≤ open,close ≤ high
            high_extra = Decimal(str(rng.uniform(0.0, 1.0))) * self.volatility * Decimal("0.5")
            low_extra = Decimal(str(rng.uniform(0.0, 1.0))) * self.volatility * Decimal("0.5")
            open_ = prev_close
            high = max(open_, new_close) + high_extra
            low = max(Decimal("0.01"), min(open_, new_close) - low_extra)
            volume = Decimal(str(rng.uniform(10.0, 1000.0)))
            yield Bar(
                symbol=self.symbol,
                open=open_,
                high=high,
                low=low,
                close=new_close,
                volume=volume,
                timestamp_utc=ts,
                timeframe=self.timeframe,
            )
            prev_close = new_close
            ts = ts + step


# ---------------------------------------------------------------------------
# In-memory replay source — used by the parity test and unit tests that
# need to share the exact same Bar list between two engines.
# ---------------------------------------------------------------------------


class InMemoryCandleSource(CandleSource):
    """Wrap an existing ``Sequence[Bar]`` as a candle source."""

    def __init__(self, bars: Iterable[Bar]) -> None:
        """Snapshot the iterable into a tuple and validate non-empty + sorted."""
        self._bars: tuple[Bar, ...] = tuple(bars)
        if not self._bars:
            msg = "InMemoryCandleSource requires at least one bar"
            raise CandleSourceError(msg)
        first = self._bars[0]
        for b in self._bars[1:]:
            if b.symbol != first.symbol:
                msg = (
                    f"InMemoryCandleSource bars must share one symbol; "
                    f"got {first.symbol!r} and {b.symbol!r}"
                )
                raise CandleSourceError(msg)
            if b.timeframe != first.timeframe:
                msg = (
                    f"InMemoryCandleSource bars must share one timeframe; "
                    f"got {first.timeframe!r} and {b.timeframe!r}"
                )
                raise CandleSourceError(msg)
        prev_ts = None
        for b in self._bars:
            if prev_ts is not None and b.timestamp_utc <= prev_ts:
                msg = (
                    f"InMemoryCandleSource bars must be strictly chronological; "
                    f"saw {prev_ts.isoformat()} then {b.timestamp_utc.isoformat()}"
                )
                raise CandleSourceError(msg)
            prev_ts = b.timestamp_utc
        self.symbol = first.symbol
        self.timeframe = first.timeframe

    def __iter__(self) -> Iterator[Bar]:
        """Yield the snapshotted bars in chronological order."""
        return iter(self._bars)

    @property
    def bars(self) -> Sequence[Bar]:
        """Return the snapshotted bars (read-only)."""
        return self._bars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_utc(name: str, v: datetime) -> None:
    """Raise unless ``v`` is timezone-aware."""
    if v.tzinfo is None:
        msg = f"{name} must be timezone-aware (UTC)"
        raise CandleSourceError(msg)


def _coerce_utc(value: object) -> datetime:
    """Coerce a pandas timestamp or python datetime into UTC-aware datetime."""
    # pandas.Timestamp is a datetime subclass, so isinstance works.
    if isinstance(value, datetime):
        ts = value
    else:  # numpy.datetime64 etc.; fall back to pandas.
        import pandas as pd

        ts = pd.Timestamp(value).to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    else:
        ts = ts.astimezone(UTC)
    return ts
