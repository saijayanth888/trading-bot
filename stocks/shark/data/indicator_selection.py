"""
Strategy-facing adapter for the LLM-driven indicator selector.

`shark.agents.market_analyst.select_indicators` returns a Pydantic
IndicatorSelection. Freqtrade strategies and Shark analysts want a plain
list of indicator ids they can iterate over. This module is the seam.

Usage from a Freqtrade strategy (no live wiring in this branch — see
HANDOFF.md for the spec)::

    from shark.data.indicator_selection import indicators_for_pair

    def feature_engineering_expand_all(self, dataframe, period, metadata, **kw):
        regime = self.detect_regime(dataframe)  # strategy-side helper
        ids = indicators_for_pair(metadata["pair"], regime)
        for ind_id in ids:
            dataframe = _apply_indicator(dataframe, ind_id, period)
        return dataframe

We deliberately keep this module thin — no LLM calls happen here, the agent
module owns caching and provider routing.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Iterable

from shark.agents.market_analyst import (
    IndicatorPick,
    IndicatorSelection,
    select_indicators,
)

logger = logging.getLogger(__name__)


def indicators_for_pair(
    pair: str,
    regime: str,
    bars: Iterable[Any] | None = None,
    *,
    use_cache: bool = True,
    on_date: _date | None = None,
    chat_json_fn: Any | None = None,
) -> list[str]:
    """Return the indicator ids the strategy should compute for `pair`.

    Thin wrapper over `select_indicators` — pulls only the `indicator` field
    out of each pick. Use `selection_for_pair` if you need the usage notes
    too (e.g. for logging / dashboards).
    """
    selection = select_indicators(
        ticker=pair,
        regime=regime,
        bars=bars,
        use_cache=use_cache,
        on_date=on_date,
        chat_json_fn=chat_json_fn,
    )
    return [pick.indicator for pick in selection.picks]


def selection_for_pair(
    pair: str,
    regime: str,
    bars: Iterable[Any] | None = None,
    *,
    use_cache: bool = True,
    on_date: _date | None = None,
    chat_json_fn: Any | None = None,
) -> IndicatorSelection:
    """Return the full IndicatorSelection (with usage notes)."""
    return select_indicators(
        ticker=pair,
        regime=regime,
        bars=bars,
        use_cache=use_cache,
        on_date=on_date,
        chat_json_fn=chat_json_fn,
    )


def picks_as_dict(selection: IndicatorSelection) -> dict[str, str]:
    """Flatten a selection to {indicator_id: usage_note}."""
    return {p.indicator: p.why for p in selection.picks}


__all__ = [
    "indicators_for_pair",
    "selection_for_pair",
    "picks_as_dict",
    "IndicatorPick",
    "IndicatorSelection",
]
