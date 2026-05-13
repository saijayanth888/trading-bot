"""Parity oracle — compare freqtrade and V4 decisions for shadow-mode QA.

The cutover gate (see `docs/V4_SHADOW_MODE_DESIGN.md`) requires V4 to
agree with freqtrade ≥85% of the time over a rolling window before V4
is promoted to live. This module computes the per-decision verdict;
aggregation into rolling windows lives in the (future) parity-oracle
cron that reads from both ledgers and writes diffs to
`user_data/v4_runtime/parity.jsonl`.

Stdlib-only; safe to import early.
"""
from __future__ import annotations

from typing import Any

_VALID_SIDES: frozenset[str] = frozenset({"LONG", "SHORT", "FLAT"})


def compare_decisions(
    freqtrade: dict[str, Any],
    v4: dict[str, Any],
) -> dict[str, Any]:
    """Compare a freqtrade decision against a V4 shadow decision.

    Both inputs are normalized dicts with at least a ``side`` field
    (one of LONG/SHORT/FLAT; missing is treated as FLAT). The returned
    dict carries the verdict plus the inputs for telemetry.

    Verdict semantics:
        - "agree"    — same side on both, including same-FLAT
        - "conflict" — opposite directional sides (LONG vs SHORT)
        - "abstain"  — one side is FLAT, the other is directional

    Raises:
        ValueError: if either ``side`` is non-empty but not in
                    {LONG, SHORT, FLAT}.
    """
    f_side = freqtrade.get("side") or "FLAT"
    v_side = v4.get("side") or "FLAT"

    if f_side not in _VALID_SIDES or v_side not in _VALID_SIDES:
        raise ValueError(
            f"unknown side: freqtrade={f_side!r} v4={v_side!r}; "
            f"expected one of {sorted(_VALID_SIDES)}"
        )

    if f_side == v_side:
        verdict = "agree"
    elif f_side == "FLAT" or v_side == "FLAT":
        verdict = "abstain"
    else:
        # Both directional and not equal → LONG vs SHORT (or vice versa)
        verdict = "conflict"

    return {
        "pair": freqtrade.get("pair") or v4.get("pair"),
        "freqtrade_side": f_side,
        "v4_side": v_side,
        "verdict": verdict,
    }
