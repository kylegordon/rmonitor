"""Tests for relay/main.py's post_message retry/backoff/exit behavior.

Uses lightweight fakes rather than aiohttp's own test utilities, since
post_message only needs `session.post(...)` to behave like an async
context manager yielding an object with a `.status` attribute.
"""

import asyncio
import pathlib
import sys
import threading
import types

import aiohttp
import pytest
from unittest.mock import AsyncMock

from relay import main as relay_main
from relay.class_code_client import ClassCodeStatus


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
    assert await relay_main.post_message(session, {"type": "heartbeat"}) is True
    assert session.calls == 2


@pytest.mark.asyncio
async def test_post_message_retries_on_429_then_succeeds():
    session = FakeSession([429, 429, 200])
    await relay_main.post_message(session, {"type": "heartbeat"})
    assert session.calls == 3


@pytest.mark.asyncio
async def test_post_message_drops_on_400_without_retry_or_exit():
    session = FakeSession([400])
    assert await relay_main.post_message(session, {"type": "heartbeat"}) is False
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
    ``state["batch"]`` through its callback (recording what it raised) and
    ``state["status"]`` through its status hook, then waits to be cancelled.
    The feed keeps its ``on_message`` in ``state["on_message"]`` and sets
    ``state["feed_built"]``; the class-code client records each
    ``note_session`` call in ``state["sessions"]``, raising
    ``state["session_error"]`` if one is set.  The autouse ``fast_sleep``
    makes ``asyncio.sleep`` return without yielding, so wait on an event
    here, never a sleep poll.
    """
    state = {
        "feed_done": asyncio.Event(),
        "codes_started": asyncio.Event(),
        "codes_clients": [],
        "codes_cancelled": False,
        "batch": None,
        "batch_error": None,
        "status": None,
        "on_message": None,
        "feed_built": asyncio.Event(),
        "sessions": [],
        "session_error": None,
    }

    class FakeFeed:
        def __init__(self, *args, **kwargs):
            state["on_message"] = args[2]
            state["feed_built"].set()

        async def run(self):
            await state["feed_done"].wait()

    class FakeCodes:
        def __init__(self, host, on_batch, **kwargs):
            self.host = host
            self.port = 51738
            self.on_batch = on_batch
            self.on_status = kwargs.get("on_status")
            state["codes_clients"].append(self)

        def note_session(self, number, description):
            state["sessions"].append((number, description))
            if state["session_error"] is not None:
                raise state["session_error"]

        async def run(self):
            if state["status"] is not None:
                self.on_status(state["status"])
            if state["batch"] is not None:
                try:
                    await self.on_batch(state["batch"])
                except Exception as exc:
                    state["batch_error"] = exc
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
async def test_a_rejected_class_code_post_is_a_failed_delivery(fake_sources, monkeypatch):
    """A 401 (say) is dropped for a feed message, but a class-code push is not
    repeated, so its rejection must surface for ClassCodeClient to retry."""
    post = AsyncMock(return_value=False)
    monkeypatch.setattr(relay_main, "post_message", post)
    fake_sources["batch"] = {"type": "class_codes", "run_id": "r", "entries": []}
    task = asyncio.ensure_future(relay_main.main(_relay_config()))
    await asyncio.wait_for(fake_sources["codes_started"].wait(), timeout=2.0)
    assert isinstance(fake_sources["batch_error"], ConnectionError)
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_class_code_post_failure_does_not_exit_the_relay(fake_sources, monkeypatch):
    post = AsyncMock(side_effect=SystemExit(1))
    monkeypatch.setattr(relay_main, "post_message", post)
    fake_sources["batch"] = {"type": "class_codes", "run_id": "r", "entries": []}
    task = asyncio.ensure_future(relay_main.main(_relay_config()))
    await asyncio.wait_for(fake_sources["codes_started"].wait(), timeout=2.0)
    # An ordinary error, which ClassCodeClient keeps and retries — never the
    # SystemExit that would bypass RelayRunner's respawn.
    assert isinstance(fake_sources["batch_error"], ConnectionError)
    assert post.await_count == 1
    assert not task.done()
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_a_rejected_preload_is_a_failed_delivery(fake_sources, monkeypatch):
    monkeypatch.setattr(relay_main, "post_message", AsyncMock(return_value=False))
    fake_sources["batch"] = {"type": "class_code_preload", "entries": [], "age_seconds": 0.0}
    task = asyncio.ensure_future(relay_main.main(_relay_config()))
    await asyncio.wait_for(fake_sources["codes_started"].wait(), timeout=2.0)
    assert isinstance(fake_sources["batch_error"], ConnectionError)
    assert str(fake_sources["batch_error"]) == "could not deliver class_code_preload"
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_disabled_class_codes_report_a_disabled_status(fake_sources):
    seen = []
    fake_sources["feed_done"].set()
    await asyncio.wait_for(
        relay_main.main(_relay_config(class_codes_enabled=False), on_class_codes_status=seen.append),
        timeout=2.0,
    )
    assert seen == [ClassCodeStatus("disabled")]


@pytest.mark.asyncio
async def test_a_raising_disabled_status_hook_does_not_stop_the_feed(fake_sources, caplog):
    def on_status(status):
        raise RuntimeError("window closing")

    task = asyncio.ensure_future(relay_main.main(
        _relay_config(class_codes_enabled=False), on_class_codes_status=on_status,
    ))
    await asyncio.sleep(0.05)
    assert not task.done()  # the feed is running
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)
    assert "status callback failed" in caplog.text


RUN_MESSAGE = {"type": "run", "unique_number": "5", "description": "Race 2"}


@pytest.mark.asyncio
async def test_main_feeds_each_session_record_to_the_class_code_client(
    fake_sources, monkeypatch
):
    post = AsyncMock(return_value=True)
    monkeypatch.setattr(relay_main, "post_message", post)
    task = asyncio.ensure_future(relay_main.main(_relay_config()))
    await asyncio.wait_for(fake_sources["codes_started"].wait(), timeout=2.0)
    on_message = fake_sources["on_message"]
    await on_message(RUN_MESSAGE)
    await on_message({"type": "heartbeat", "flag": "Green"})
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)
    assert fake_sources["sessions"] == [("5", "Race 2")]
    assert post.await_count == 2


@pytest.mark.asyncio
async def test_a_raising_session_hook_does_not_stop_the_feed(
    fake_sources, monkeypatch, caplog
):
    post = AsyncMock(return_value=True)
    monkeypatch.setattr(relay_main, "post_message", post)
    fake_sources["session_error"] = RuntimeError("bad table")
    task = asyncio.ensure_future(relay_main.main(_relay_config()))
    await asyncio.wait_for(fake_sources["codes_started"].wait(), timeout=2.0)
    await fake_sources["on_message"](RUN_MESSAGE)
    assert post.await_count == 1
    assert not task.done()
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)
    assert "session hook failed" in caplog.text


@pytest.mark.asyncio
async def test_session_records_are_harmless_when_class_codes_are_disabled(
    fake_sources, monkeypatch
):
    post = AsyncMock(return_value=True)
    monkeypatch.setattr(relay_main, "post_message", post)
    task = asyncio.ensure_future(
        relay_main.main(_relay_config(class_codes_enabled=False))
    )
    await asyncio.wait_for(fake_sources["feed_built"].wait(), timeout=2.0)
    await fake_sources["on_message"](RUN_MESSAGE)
    fake_sources["feed_done"].set()
    await asyncio.wait_for(task, timeout=2.0)
    assert post.await_count == 1
    assert fake_sources["sessions"] == []


def test_class_code_status_reaches_the_runner_callback(fake_sources):
    status = ClassCodeStatus("connected", preloaded=3)
    fake_sources["status"] = status
    seen = []
    arrived = threading.Event()

    def on_status(s):
        seen.append(s)
        arrived.set()

    runner = relay_main.RelayRunner(on_class_codes_status=on_status)
    runner.start(_relay_config())
    try:
        assert arrived.wait(timeout=2.0)
    finally:
        runner.stop()
    assert seen == [status]


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _version_module(version):
    module = types.ModuleType("relay._version")
    module.CURRENT_VERSION = version
    return module


def test_release_version_prefers_the_version_file_over_a_stale_version_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "relay._version", _version_module("9.9.9"))
    assert relay_main._read_release_version() == (ROOT / "VERSION").read_text().strip()


def test_release_version_falls_back_to_the_version_module(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "relay._version", _version_module("9.9.9"))
    assert relay_main._read_release_version(version_file=tmp_path / "VERSION") == "9.9.9"


def test_release_version_falls_back_to_dev_with_neither_source(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "relay._version", None)
    assert relay_main._read_release_version(version_file=tmp_path / "VERSION") == "0.0.0-dev"
