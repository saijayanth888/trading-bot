"""Tests for the Slack notifier with dedup, severity, retries."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from quanta_core.observability.notifier import (
    LogOnlyNotifier,
    NotifierError,
    Severity,
    SlackNotifier,
)


class _Recorder:
    """Stateful httpx mock that records every request + replays scripted responses."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._responses: list[httpx.Response] = []

    def queue(self, *responses: httpx.Response) -> None:
        self._responses.extend(responses)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "json": request.read().decode("utf-8"),
            }
        )
        if not self._responses:
            return httpx.Response(200, json={"ok": True})
        return self._responses.pop(0)


@pytest_asyncio.fixture()
async def recorder() -> AsyncIterator[_Recorder]:
    rec = _Recorder()
    yield rec


@pytest_asyncio.fixture()
async def slack_client(recorder: _Recorder) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.MockTransport(recorder.handler)
    client = httpx.AsyncClient(transport=transport, timeout=2.0)
    try:
        yield client
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- LogOnly


async def test_log_only_emits_at_each_severity(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="quanta_core.observability.notifier")
    notifier = LogOnlyNotifier()
    for sev in Severity:
        ok = await notifier.notify("hello", severity=sev)
        assert ok is True
    records = [r for r in caplog.records if r.getMessage().startswith("notifier_message")]
    assert len(records) == len(list(Severity))


async def test_log_only_dedup_suppresses_duplicates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="quanta_core.observability.notifier")
    notifier = LogOnlyNotifier(dedup_window_s=60)
    assert await notifier.notify("hi", dedup_key="k1") is True
    assert await notifier.notify("hi", dedup_key="k1") is False
    # Severity bumps the dedup key, allowing through.
    assert await notifier.notify("hi", severity=Severity.ERROR, dedup_key="k1") is True


async def test_log_only_dedup_disabled_when_no_key() -> None:
    notifier = LogOnlyNotifier()
    assert await notifier.notify("a") is True
    assert await notifier.notify("a") is True


def test_dedup_window_must_be_non_negative() -> None:
    with pytest.raises(NotifierError):
        LogOnlyNotifier(dedup_window_s=-1)


async def test_clear_dedup_cache() -> None:
    notifier = LogOnlyNotifier()
    assert await notifier.notify("a", dedup_key="x") is True
    assert await notifier.notify("a", dedup_key="x") is False
    notifier.clear_dedup_cache()
    assert await notifier.notify("a", dedup_key="x") is True


async def test_severity_must_be_enum() -> None:
    notifier = LogOnlyNotifier()
    with pytest.raises(NotifierError):
        await notifier.notify("a", severity="info")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Slack happy path


async def test_slack_notifier_posts_json(
    slack_client: httpx.AsyncClient,
    recorder: _Recorder,
) -> None:
    notifier = SlackNotifier(
        webhook_url="https://example.com/hook",
        client=slack_client,
    )
    ok = await notifier.notify(
        "hello",
        severity=Severity.INFO,
        context={"symbol": "BTC/USD"},
    )
    assert ok is True
    assert len(recorder.requests) == 1
    payload = recorder.requests[0]["json"]
    assert "INFO" in payload
    assert "BTC/USD" in payload
    assert "<!channel>" not in payload


async def test_slack_notifier_atchannel_on_critical(
    slack_client: httpx.AsyncClient,
    recorder: _Recorder,
) -> None:
    notifier = SlackNotifier(
        webhook_url="https://example.com/hook",
        client=slack_client,
    )
    await notifier.notify("kill switch", severity=Severity.CRITICAL)
    payload = recorder.requests[0]["json"]
    assert "<!channel>" in payload
    assert "CRITICAL" in payload


async def test_slack_notifier_retries_on_5xx(
    slack_client: httpx.AsyncClient,
    recorder: _Recorder,
) -> None:
    recorder.queue(
        httpx.Response(503, text="oops"),
        httpx.Response(503, text="oops"),
        httpx.Response(200, text="ok"),
    )
    notifier = SlackNotifier(
        webhook_url="https://example.com/hook",
        client=slack_client,
        max_retries=2,
    )
    ok = await notifier.notify("retry me", severity=Severity.WARN)
    assert ok is True
    assert len(recorder.requests) == 3


async def test_slack_notifier_swallows_transport_errors(
    recorder: _Recorder,
) -> None:
    async def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    try:
        notifier = SlackNotifier(
            webhook_url="https://example.com/hook",
            client=client,
            max_retries=1,
        )
        ok = await notifier.notify("dead", severity=Severity.WARN)
        assert ok is False
    finally:
        await client.aclose()


async def test_slack_notifier_swallows_terminal_5xx(
    slack_client: httpx.AsyncClient,
    recorder: _Recorder,
) -> None:
    recorder.queue(
        httpx.Response(503, text="oops"),
        httpx.Response(503, text="oops"),
    )
    notifier = SlackNotifier(
        webhook_url="https://example.com/hook",
        client=slack_client,
        max_retries=1,
    )
    ok = await notifier.notify("still dead", severity=Severity.WARN)
    assert ok is False


async def test_slack_notifier_swallows_4xx_after_raise(
    recorder: _Recorder,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        notifier = SlackNotifier(
            webhook_url="https://example.com/hook",
            client=client,
        )
        ok = await notifier.notify("4xx", severity=Severity.WARN)
        assert ok is False
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- config validation


def test_slack_rejects_non_http_url() -> None:
    with pytest.raises(NotifierError):
        SlackNotifier("ftp://example.com/hook")
    with pytest.raises(NotifierError):
        SlackNotifier("")


def test_slack_rejects_bad_retry_or_timeout() -> None:
    with pytest.raises(NotifierError):
        SlackNotifier("https://x", max_retries=-1)
    with pytest.raises(NotifierError):
        SlackNotifier("https://x", timeout_s=0)


async def test_slack_aclose_when_client_owned() -> None:
    notifier = SlackNotifier("https://example.com/hook")
    await notifier.aclose()


async def test_slack_aclose_when_client_external(
    slack_client: httpx.AsyncClient,
) -> None:
    notifier = SlackNotifier(webhook_url="https://example.com/hook", client=slack_client)
    await notifier.aclose()
    # External client must still be usable.
    assert not slack_client.is_closed


# --------------------------------------------------------------------------- dedup window expiry


async def test_dedup_window_zero_disables_window(
    slack_client: httpx.AsyncClient,
    recorder: _Recorder,
) -> None:
    notifier = SlackNotifier(
        webhook_url="https://example.com/hook",
        client=slack_client,
        dedup_window_s=0,
    )
    await notifier.notify("a", dedup_key="x")
    await notifier.notify("a", dedup_key="x")
    # Both messages went through.
    assert len(recorder.requests) == 2
