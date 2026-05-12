"""Pre-flight slippage gate.

A pure function: feed it the proposal, the current market mid, and the
threshold; receive a structured pass/fail with a reason string. No I/O,
no clock dependence (the caller injects ``now`` so the staleness check is
testable).

Edge cases
----------
* **Stale quote** (mid timestamp older than ``max_quote_age_s``) → fail with
  ``"stale_quote"``. This is the most important guardrail; in practice
  >50% of "weird fills" in 2026-04 traced back to an L1 feed that had
  silently disconnected.
* **Market orders** (``proposal.limit_px is None``) → pass with reason
  ``"no_gate_market_order"``. The gate is for limit-cross protection only.
* **Non-positive mid or signal price** → fail with ``"invalid_prices"``.

The threshold is expressed as a percentage of the signal price, matching
the convention in the legacy ``execution_engine.py``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from quanta_core.execution.engine import OrderProposal

__all__ = ["SlippageGateResult", "passes"]


class SlippageGateResult(BaseModel):
    """Outcome of the slippage gate. ``ok`` is the only field most callers
    inspect; ``reason`` and ``drift_pct`` are for logs + audit."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    reason: str
    drift_pct: Decimal | None = None


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def passes(
    proposal: OrderProposal,
    current_mid: Decimal,
    threshold_pct: Decimal,
    *,
    quote_ts: dt.datetime,
    now: dt.datetime,
    max_quote_age_s: float = 5.0,
) -> SlippageGateResult:
    """Decide whether to place ``proposal`` given the current ``current_mid``.

    Parameters
    ----------
    proposal
        The order we are about to send. The fields read are ``limit_px``
        and ``signal_px``.
    current_mid
        The current best-mid (or top-of-book best-side; the caller decides
        which side to feed in). Same currency as ``proposal.limit_px``.
    threshold_pct
        Maximum permitted drift, in **percent** (not decimal). ``0.5`` means
        0.5 %. A value of ``0`` disables the gate.
    quote_ts
        Timestamp on the L1 quote that produced ``current_mid``.
    now
        Wall-clock used for staleness comparison.
    max_quote_age_s
        Reject as ``stale_quote`` if ``now - quote_ts`` exceeds this.

    Returns
    -------
    SlippageGateResult
        ``ok=True`` means the engine may place the order. ``ok=False``
        means reject locally; ``reason`` carries the machine-readable code.
    """
    # Market orders bypass the gate; there's no price to compare to.
    if proposal.limit_px is None:
        return SlippageGateResult(ok=True, reason="no_gate_market_order")

    # Stale-quote guard MUST run before price math: a stale mid is worse than
    # no mid, because it looks credible.
    age = (now - quote_ts).total_seconds()
    if age > max_quote_age_s:
        return SlippageGateResult(ok=False, reason="stale_quote")
    if age < 0:
        # Clock-skew: quote ts is in the future. Treat as stale; safer than
        # trusting it.
        return SlippageGateResult(ok=False, reason="stale_quote")

    signal_px = proposal.signal_px
    if signal_px <= 0 or current_mid <= 0:
        return SlippageGateResult(ok=False, reason="invalid_prices")

    if threshold_pct <= 0:
        # Gate disabled by config — but we still record the drift for audit.
        drift = (current_mid - signal_px).copy_abs() / signal_px * Decimal("100")
        return SlippageGateResult(ok=True, reason="gate_disabled", drift_pct=drift)

    drift = (current_mid - signal_px).copy_abs() / signal_px * Decimal("100")
    if drift > threshold_pct:
        return SlippageGateResult(ok=False, reason="drift_exceeds_threshold", drift_pct=drift)

    return SlippageGateResult(ok=True, reason="ok", drift_pct=drift)
