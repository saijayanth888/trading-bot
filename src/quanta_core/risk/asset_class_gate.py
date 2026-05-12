"""Asset-class + ownership gate (derived from the 2026-05-12 Shark/Wheel leak fix).

Background
----------
Shark and Wheel trade on the same Alpaca paper account. Shark is the
swing-trader (US equities only); Wheel is the CSP/CC option strategy
that can also hold *assigned shares* in the same account. A 2026-05-12
midday cut closed five Wheel-owned long puts because Shark's hard-stop
loop walked the full position list filtered only by ``asset_class``.

The leak fix in ``stocks/shark/phases/midday.py`` combined two gates:

1. **Asset-class filter** — skip non-equity rows (options belong to Wheel).
2. **Ownership filter** — skip equity rows that aren't in Shark's owned set
   (e.g. assigned shares from Wheel CSPs).

This module exposes the same logic as a **pure function**
:func:`is_quanta_managed` over a typed :class:`Position` dataclass so
the quanta_core stack can call it from any submodule without re-importing
the Shark phase module.

Rules
-----
Given a candidate subsystem ``"shark"`` or ``"wheel"`` asking "do I own
this position?":

* If the position's ``asset_class`` is **not** ``us_equity``:

  * Return ``True`` iff the asking subsystem is ``"wheel"``.
    (Options are Wheel's responsibility by stack-level convention.)
  * Return ``False`` otherwise (Shark must never touch options).

* If the position is ``us_equity``:

  * Consult the per-subsystem ownership ledger via
    :func:`quanta_core.risk.ownership.load_owned`. Return ``True`` iff
    the symbol is present in that subsystem's owned set.
  * If the ownership ledger is unavailable (file missing / empty), the
    fail-safe default is **``False``** — refuse to act rather than
    silently mis-attribute the row. The caller is expected to handle a
    ``False`` here by skipping the position, not by acting on it.

The function never raises. Errors loading the ownership file degrade
to ``False``; see :mod:`quanta_core.risk.ownership` for the file format.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from quanta_core.risk.ownership import Subsystem, load_owned, owns

logger = logging.getLogger(__name__)

__all__ = [
    "Position",
    "is_quanta_managed",
]


# Standard Alpaca-style asset class strings. The dataclass accepts a plain
# ``str`` so non-standard exchanges can still feed in their own values; the
# gate logic only special-cases ``"us_equity"``.
AssetClass = Literal["us_equity", "us_option", "crypto", "etf"]


@dataclass(frozen=True)
class Position:
    """A position row as the strategy layer sees it.

    The dataclass is intentionally minimal — only the fields the gate
    consumes. Strategy / venue adapters add their own typed views (with
    qty, cost basis, mark, etc.) on top of this primitive.

    Parameters
    ----------
    symbol :
        Ticker (equities: ``"NVDA"``; options: OCC string, e.g.
        ``"AAPL250620P00150000"``). Case-insensitive; canonicalised to
        upper-case during ownership lookups.
    asset_class :
        ``"us_equity"`` for shares, ``"us_option"`` (or anything other
        than ``"us_equity"``) for options/futures. Free-form string so
        venue values pass through.
    venue :
        Optional venue tag, e.g. ``"alpaca"`` / ``"coinbase"``. Not
        consumed by the gate today but kept on the dataclass so the
        observability layer can render it without re-querying the venue.
    """

    symbol: str
    asset_class: str = "us_equity"
    venue: str | None = None


def is_quanta_managed(position: Position, subsystem: Subsystem) -> bool:
    """Decide whether *position* belongs to *subsystem* under quanta_core rules.

    See module docstring for the full rule set. Summary:

    * Non-equity row → ``True`` only when ``subsystem == "wheel"``.
    * Equity row → ``True`` iff ``position.symbol`` is in the subsystem's
      ownership ledger (see :mod:`quanta_core.risk.ownership`).

    The function is pure-functional w.r.t. its arguments + the on-disk
    state for *subsystem*. It never mutates state and never raises.
    """
    if not position.symbol:
        return False

    asset_class = (position.asset_class or "us_equity").lower()

    if asset_class != "us_equity":
        # Non-equity: only Wheel ever manages options / non-stock rows.
        # Shark must hard-skip — that was the 2026-05-12 leak's root cause.
        if subsystem == "wheel":
            return True
        logger.debug(
            "[asset_class_gate] %s asks for non-equity %s (asset_class=%s) — refused",
            subsystem,
            position.symbol,
            position.asset_class,
        )
        return False

    # Equity row: consult ownership ledger. Errors fall through to False
    # by way of ``load_owned`` returning an empty set on corrupt files.
    try:
        owned = load_owned(subsystem)
    except (OSError, ValueError) as exc:
        # load_owned should never raise on disk errors (it logs + returns
        # an empty set), but we belt-and-braces here so the gate can never
        # blow up the caller.
        logger.warning(
            "[asset_class_gate] ownership lookup failed for %s/%s: %s — refusing",
            subsystem,
            position.symbol,
            exc,
        )
        return False

    return owns(owned, position.symbol)
