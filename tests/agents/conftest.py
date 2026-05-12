"""Shared fixtures + fake Ollama client for the agents test suite.

No real network calls. Every test in this directory uses :class:`FakeOllama`
and asserts the orchestrator never tries to touch a real endpoint.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

# Make `src/` importable without a global pip install.
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quanta_core.agents import (  # noqa: E402  (after sys.path edit)
    AccountState,
    AgentVote,
    ArbiterSynthesis,
    Direction,
    MicroState,
    RiskState,
    RoleName,
    SetupContext,
)

# ---------------------------------------------------------------------------
# Fake Ollama client
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeOllama:
    """In-memory stand-in for the real Ollama SDK.

    Configure responses with :meth:`set_vote`, :meth:`set_arbiter`, or
    :meth:`set_vote_error` / :meth:`set_arbiter_error`. The fake records every
    call site so tests can assert ordering / count.
    """

    # role -> deque-ish list of responses (popped left-to-right). A list[None]
    # entry triggers ``asyncio.TimeoutError``; an Exception entry is raised.
    _vote_queue: dict[RoleName, list[AgentVote | Exception | float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _arbiter_queue: list[ArbiterSynthesis | Exception | float] = field(
        default_factory=list
    )

    calls: list[str] = field(default_factory=list)

    def set_vote(self, role: RoleName, *responses: AgentVote | Exception | float) -> None:
        """Queue one-or-more responses for ``role``. Each call pops one.

        - ``AgentVote`` returns it.
        - ``Exception`` raises it.
        - ``float`` sleeps that many seconds before returning ABSTAIN (used to
          force the orchestrator's hard timeout path).
        """
        self._vote_queue[role].extend(responses)

    def set_arbiter(self, *responses: ArbiterSynthesis | Exception | float) -> None:
        self._arbiter_queue.extend(responses)

    async def vote(
        self,
        *,
        role: RoleName,
        model: str,
        prompt: str,
        timeout_s: float,
    ) -> AgentVote:
        self.calls.append(f"vote:{role.value}")
        queue = self._vote_queue.get(role)
        if not queue:
            raise AssertionError(f"FakeOllama: no canned response for {role}")
        resp = queue.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, (int, float)):
            await asyncio.sleep(float(resp))
            return _abstain(role)
        return resp

    async def arbiter(
        self,
        *,
        model: str,
        prompt: str,
        timeout_s: float,
    ) -> ArbiterSynthesis:
        self.calls.append("arbiter")
        if not self._arbiter_queue:
            raise AssertionError("FakeOllama: no canned arbiter response")
        resp = self._arbiter_queue.pop(0)
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, (int, float)):
            await asyncio.sleep(float(resp))
            return ArbiterSynthesis(synthesis_rationale="(slept)", timed_out=True)
        return resp


def _abstain(role: RoleName) -> AgentVote:
    return AgentVote(
        role=role, direction=Direction.FLAT, conviction=0.0, abstained=True
    )


# ---------------------------------------------------------------------------
# Risk + micro gate fakes
# ---------------------------------------------------------------------------


def make_risk_gate(
    state: RiskState | None = None,
    *,
    raise_exc: BaseException | None = None,
) -> Callable[[SetupContext], Awaitable[RiskState]]:
    async def _gate(_setup: SetupContext) -> RiskState:
        if raise_exc is not None:
            raise raise_exc
        return state or RiskState(
            veto=False,
            drawdown_4w_pct=0.02,
            proposed_size_usd=1000.0,
            expected_loss_usd=20.0,
        )
    return _gate


def make_micro_gate(
    state: MicroState | None = None,
    *,
    raise_exc: BaseException | None = None,
) -> Callable[[SetupContext], Awaitable[MicroState]]:
    async def _gate(_setup: SetupContext) -> MicroState:
        if raise_exc is not None:
            raise raise_exc
        return state or MicroState(
            veto=False,
            spread_bps=1.5,
            spread_ratio_vs_median=1.0,
            depth_ratio_vs_target=2.0,
        )
    return _gate


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2026, 5, 12, 14, 30, tzinfo=UTC)
_FIXED_UUID = UUID("12345678-1234-5678-1234-567812345678")


@pytest.fixture
def setup_ctx() -> SetupContext:
    return SetupContext(
        decision_id=_FIXED_UUID,
        ts=_FIXED_TS,
        symbol="BTC-USD",
        state_snapshot_hash="abcd1234deadbeef",
        regime_features={"trend": 0.8, "vol_z": -0.2},
        last_bars=[{"o": 60000, "h": 60100, "l": 59950, "c": 60050, "v": 12.0}],
        kb_bull_context=["KB-bull-1", "KB-bull-2"],
        kb_bear_context=["KB-bear-1"],
        microstructure={"spread_bps": 1.5, "depth_top": 25.0},
        account_state=AccountState(
            equity_usd=100_000.0,
            drawdown_4w_pct=0.02,
            open_positions=1,
            cash_secured_ratio=0.3,
        ),
    )


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def vote_factory() -> Callable[..., AgentVote]:
    """Factory that builds AgentVotes with sensible defaults."""

    def _make(
        role: RoleName,
        direction: Direction = Direction.LONG,
        conviction: float = 0.8,
        abstained: bool = False,
        evidence: tuple[str, ...] | None = None,
    ) -> AgentVote:
        return AgentVote(
            role=role,
            direction=direction,
            conviction=conviction,
            abstained=abstained,
            rationale=f"{role.value} test rationale",
            evidence_keys=evidence or (f"kb:{role.value}:1",),
        )

    return _make


@pytest.fixture
def arbiter_synthesis() -> ArbiterSynthesis:
    return ArbiterSynthesis(
        synthesis_rationale="Bull and bear converge on direction; no inconsistencies.",
        agreement_pattern="unanimous",
    )
