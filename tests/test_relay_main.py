"""Tests for relay/main.py's post_message retry/backoff/exit behavior.

Uses lightweight fakes rather than aiohttp's own test utilities, since
post_message only needs `session.post(...)` to behave like an async
context manager yielding an object with a `.status` attribute.
"""

import asyncio

import aiohttp
import pytest
from unittest.mock import AsyncMock

from relay import main as relay_main


class FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ExceptionCM:
    """Mimics a request context manager that fails before yielding a response."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Returns a queued sequence of statuses/exceptions for successive .post() calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            return _ExceptionCM(resp)
        return FakeResponse(resp)


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """Avoid real retry delays slowing the test suite down."""
    monkeypatch.setattr(relay_main.asyncio, "sleep", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_post_message_exits_on_client_error():
    session = FakeSession([aiohttp.ClientError("boom")])
    with pytest.raises(SystemExit) as exc_info:
        await relay_main.post_message(session, {"type": "heartbeat"})
    assert exc_info.value.code == 1
    assert session.calls == 1


@pytest.mark.asyncio
async def test_post_message_exits_on_timeout():
    session = FakeSession([asyncio.TimeoutError()])
    with pytest.raises(SystemExit) as exc_info:
        await relay_main.post_message(session, {"type": "heartbeat"})
    assert exc_info.value.code == 1
    assert session.calls == 1


@pytest.mark.asyncio
async def test_post_message_retries_on_5xx_then_succeeds():
    session = FakeSession([503, 200])
    await relay_main.post_message(session, {"type": "heartbeat"})
    assert session.calls == 2


@pytest.mark.asyncio
async def test_post_message_retries_on_429_then_succeeds():
    session = FakeSession([429, 429, 200])
    await relay_main.post_message(session, {"type": "heartbeat"})
    assert session.calls == 3


@pytest.mark.asyncio
async def test_post_message_drops_on_400_without_retry_or_exit():
    session = FakeSession([400])
    await relay_main.post_message(session, {"type": "heartbeat"})
    assert session.calls == 1


@pytest.mark.asyncio
async def test_post_message_exits_after_max_retriable_attempts(monkeypatch):
    """A server that stays degraded forever must not wedge the relay's TCP
    reader indefinitely — after RETRY_MAX_ATTEMPTS the relay exits so the
    container restart policy can recover, the same as a connection failure."""
    monkeypatch.setattr(relay_main, "RETRY_MAX_ATTEMPTS", 3)
    session = FakeSession([503, 503, 503, 503, 503])
    with pytest.raises(SystemExit) as exc_info:
        await relay_main.post_message(session, {"type": "heartbeat"})
    assert exc_info.value.code == 1
    assert session.calls == 3


@pytest.mark.asyncio
async def test_post_message_calls_on_attempt_once_per_post_including_retries():
    calls = []
    session = FakeSession([503, 200])
    await relay_main.post_message(
        session, {"type": "heartbeat"}, on_attempt=lambda: calls.append(1)
    )
    assert len(calls) == 2
