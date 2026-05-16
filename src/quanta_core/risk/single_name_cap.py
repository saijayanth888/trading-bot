"""Single-name cap enforcement — B8 root-cause fix.

The legacy ``unified_risk.evaluate_single_name_cap`` function exists, but it
runs *post-fill* (in the sizing review path) and only ever clipped the
intended notional. Bug B8 in the 2026-05-16 dashboard redesign spec
documents an actual incident where BTC reached 34× the configured cap —
the gate was bypassed because nothing on the live fill-emission path
called it.

This module ships the **entry-time** enforcement. It is the single
public function the live dispatcher wires up *before* an OrderProposal
reaches the execution engine. It is pure (no DB writes other than the
append-only ``risk_alerts.jsonl`` audit trail) and returns a tuple so
callers can short-circuit on ``allowed == False`` without unpacking
dicts.

Contract:

    >>> allowed, reason = enforce_single_name_cap(
    ...     symbol="BTC/USD",
    ...     stake_usd=66212.89,
    ...     sleeve_equity_usd=19000.0,
    ...     cap_pct=0.10,
    ... )
    >>> allowed
    False
    >>> "BTC/USD" in reason
    True

When the call rejects, an ``risk_alert`` JSON line is appended to
``user_data/data/risk_alerts.jsonl`` (creating the file if absent). The
file is **append-only** — nothing in this module ever opens it with
``"w"`` (spec §5.4 hard constraint).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audit-trail path resolution
# ---------------------------------------------------------------------------

# In-container path: USER_DATA_ROOT defaults to /app/user_data and the
# dashboard container bind-mounts the host's user_data/ there. Host-side
# (tests, scripts) falls back to the repo-relative path. The file is
# created on first append; we never touch it with "w".
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_DEFAULT_DATA_ROOT: Final[Path] = _REPO_ROOT / "user_data" / "data"


def _alerts_path() -> Path:
    root = Path(os.environ.get("USER_DATA_ROOT", str(_DEFAULT_DATA_ROOT.parent)))
    return root / "data" / "risk_alerts.jsonl"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enforce_single_name_cap(
    symbol: str,
    stake_usd: float,
    sleeve_equity_usd: float,
    cap_pct: float,
    *,
    sleeve: str = "crypto",
    append_alert: bool = True,
) -> tuple[bool, str]:
    """Reject single-name stakes that exceed ``cap_pct`` × sleeve equity.

    Parameters
    ----------
    symbol
        e.g. ``"BTC/USD"``, ``"SOFI"``. Used for audit + alert payload only.
    stake_usd
        Notional dollar size of the *proposed* fill (price × qty). Must be
        positive — non-positive sizes are accepted as a no-op (no enforcement
        on closes; this is an entry-time gate).
    sleeve_equity_usd
        Current equity allocated to the sleeve (crypto book equity for
        crypto fills, stocks book equity for stocks). Non-positive values
        always reject (cannot enter when no equity).
    cap_pct
        Fraction of sleeve equity allowed in a single name. ``0.10`` ==
        10%. Values outside ``[0, 1]`` clamp to 1.0 (no-op).
    sleeve
        Free-form tag persisted in the alert row — ``"crypto"`` /
        ``"stocks"`` / ``"shark"``. Defaults to ``"crypto"``.
    append_alert
        When ``True`` (default) rejections append a ``risk_alert`` row to
        ``user_data/data/risk_alerts.jsonl``. Set ``False`` in unit tests
        to avoid touching disk.

    Returns
    -------
    (allowed, reason)
        ``allowed`` is ``True`` when the stake is within cap; ``False``
        when the dispatcher MUST refuse to forward the proposal.
        ``reason`` is operator-readable.
    """
    # Defensive normalisation — non-positive stake is a close/reduce path,
    # which is never gated by the single-name cap.
    if stake_usd <= 0:
        return True, "stake non-positive (close/reduce path; cap not applied)"

    # Non-positive sleeve equity: refuse, can't open.
    if sleeve_equity_usd <= 0:
        reason = (
            f"single_name_cap REJECT {symbol}: sleeve equity non-positive "
            f"(${sleeve_equity_usd:.2f}) — cannot open new positions"
        )
        if append_alert:
            _append_alert(
                symbol=symbol,
                stake_usd=stake_usd,
                sleeve_equity_usd=sleeve_equity_usd,
                cap_pct=cap_pct,
                cap_usd=0.0,
                reason=reason,
                sleeve=sleeve,
                severity="critical",
            )
        return False, reason

    # Clamp pathological cap_pct values.
    cap_pct_eff = float(cap_pct)
    if cap_pct_eff < 0:
        cap_pct_eff = 0.0
    if cap_pct_eff > 1.0:
        cap_pct_eff = 1.0

    cap_usd = sleeve_equity_usd * cap_pct_eff
    if stake_usd > cap_usd:
        ratio = stake_usd / cap_usd if cap_usd > 0 else float("inf")
        reason = (
            f"single_name_cap REJECT {symbol}: stake ${stake_usd:,.2f} > cap "
            f"${cap_usd:,.2f} ({cap_pct_eff*100:.1f}% of ${sleeve_equity_usd:,.2f} "
            f"sleeve equity) — {ratio:.2f}× over cap"
        )
        # Severity ramp: 1.5× → warning, 2× → critical (matches B8 incident
        # where the 34× breach should have screamed red, not whispered amber).
        severity = "critical" if ratio >= 2.0 else "warning"
        if append_alert:
            _append_alert(
                symbol=symbol,
                stake_usd=stake_usd,
                sleeve_equity_usd=sleeve_equity_usd,
                cap_pct=cap_pct_eff,
                cap_usd=cap_usd,
                reason=reason,
                sleeve=sleeve,
                severity=severity,
            )
        return False, reason

    return True, (
        f"single_name_cap OK {symbol}: ${stake_usd:,.2f} ≤ cap ${cap_usd:,.2f} "
        f"({cap_pct_eff*100:.1f}% of sleeve)"
    )


# ---------------------------------------------------------------------------
# Append-only audit trail (spec §5.4 — never opens with "w")
# ---------------------------------------------------------------------------


def _append_alert(
    *,
    symbol: str,
    stake_usd: float,
    sleeve_equity_usd: float,
    cap_pct: float,
    cap_usd: float,
    reason: str,
    sleeve: str,
    severity: str,
) -> None:
    """Append a single JSON line to ``user_data/data/risk_alerts.jsonl``.

    Open mode is *always* ``"a"`` per spec §5.4. The file is created on
    first append; the directory is also auto-created so the function is
    safe in a fresh container.
    """
    payload = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "kind": "single_name_cap_breach",
        "severity": severity,
        "symbol": symbol,
        "sleeve": sleeve,
        "stake_usd": round(float(stake_usd), 2),
        "sleeve_equity_usd": round(float(sleeve_equity_usd), 2),
        "cap_pct": round(float(cap_pct), 6),
        "cap_usd": round(float(cap_usd), 2),
        "reason": reason,
    }
    try:
        path = _alerts_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # APPEND-ONLY (spec §5.4 hard constraint — never "w").
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
    except OSError as exc:
        # Never let an audit-trail failure block the safety gate from
        # returning False. The gate's correctness is more important than
        # the audit row; the rejection still propagates.
        logger.warning(
            "single_name_cap: alert append failed (%s) — gate decision unchanged",
            exc,
        )


__all__ = ["enforce_single_name_cap"]
