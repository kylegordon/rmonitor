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


# ---------------------------------------------------------------------------
# main(): the class-code client runs beside the feed, isolated from it
# ---------------------------------------------------------------------------

def _relay_config(**overrides):
    values = dict(
        host="10.0.0.9",
        port=50000,
        feed_read_timeout=30.0,
        server_url="http://localhost:8080",
        relay_secret="s",
        post_timeout=5.0,
        retry_delay=1.0,
        retry_max_delay=30.0,
        retry_max_attempts=30,
    )
    values.update(overrides)
    return relay_main.RelayConfig(**values)


@pytest.fixture
def fake_sources(monkeypatch):
    """Replace both relay sources with fakes driven by events.

    The feed's ``run()`` returns once ``state["feed_done"]`` is set; the
    class-code client's ``run()`` records its start, optionally delivers
    ``state["batch"]`` through its callback, then waits to be cancelled.
    """
    state = {
        "feed_done": asyncio.Event(),
        "codes_started": asyncio.Event(),
        "codes_clients": [],
        "codes_cancelled": False,
        "batch": None,
        "batch_returned": False,
    }

    class FakeFeed:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            await state["feed_done"].wait()

    class FakeCodes:
        def __init__(self, host, on_batch, **kwargs):
            self.host = host
            self.port = 51738
            self.on_batch = on_batch
            state["codes_clients"].append(self)

        async def run(self):
            if state["batch"] is not None:
                await self.on_batch(state["batch"])
                state["batch_returned"] = True
            state["codes_started"].set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state["codes_cancelled"] = True
                raise

    monkeypatch.setattr(relay_main, "RMonitorClient", FakeFeed)
    monkeypatch.setattr(relay_main, "ClassCodeClient", FakeCodes)
    return state


@pytest.mark.asyncio
async def test_main_runs_the_class_code_client_beside_the_feed(fake_sources):
    task = asyncio.ensure_future(relay_main.main(_relay_config()))
    await asyncio.wait_for(fake_sources["codes_started"].wait(), timeout=2.0)
    assert not task.done()
    assert [c.host for c in fake_sources["codes_clients"]] == ["10.0.0.9"]
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_main_cancels_the_class_code_task_when_the_feed_loop_ends(fake_sources):
    task = asyncio.ensure_future(relay_main.main(_relay_config()))
    await asyncio.wait_for(fake_sources["codes_started"].wait(), timeout=2.0)
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)
    assert fake_sources["codes_cancelled"]


@pytest.mark.asyncio
async def test_main_does_not_start_the_class_code_client_when_disabled(fake_sources):
    fake_sources["feed_done"].set()
    await asyncio.wait_for(
        relay_main.main(_relay_config(class_codes_enabled=False)), timeout=2.0
    )
    assert fake_sources["codes_clients"] == []


@pytest.mark.asyncio
async def test_a_class_code_post_failure_does_not_exit_the_relay(fake_sources, monkeypatch):
    post = AsyncMock(side_effect=SystemExit(1))
    monkeypatch.setattr(relay_main, "post_message", post)
    fake_sources["batch"] = {"type": "class_codes", "run_id": "r", "entries": []}
    task = asyncio.ensure_future(relay_main.main(_relay_config()))
    await asyncio.wait_for(fake_sources["codes_started"].wait(), timeout=2.0)
    assert fake_sources["batch_returned"]
    assert post.await_count == 1
    assert not task.done()
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)
