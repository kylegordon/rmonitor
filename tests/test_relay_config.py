"""Tests for relay/main.py's RelayConfig and RelayRunner.

RelayRunner tests replace relay.main.main with a fake coroutine (no real
TCP/HTTP connection) so the background-thread/event-loop/task-cancellation
lifecycle can be exercised in isolation.
"""

import asyncio
import time

import pytest

from relay import main as relay_main
from relay.main import RelayConfig, RelayRunner


def _config(**overrides):
    values = dict(
        host="127.0.0.1",
        port=50000,
        feed_read_timeout=30.0,
        server_url="http://localhost:8080",
        relay_secret="",
        post_timeout=5.0,
        retry_delay=1.0,
        retry_max_delay=30.0,
        retry_max_attempts=30,
    )
    values.update(overrides)
    return RelayConfig(**values)


def test_relay_config_from_env_reflects_current_module_globals(monkeypatch):
    monkeypatch.setattr(relay_main, "RMONITOR_HOST", "10.0.0.5")
    monkeypatch.setattr(relay_main, "RMONITOR_PORT", 12345)
    monkeypatch.setattr(relay_main, "FEED_READ_TIMEOUT", 15.0)
    monkeypatch.setattr(relay_main, "SERVER_URL", "https://example.com")
    monkeypatch.setattr(relay_main, "RELAY_SECRET", "s3cr3t")
    monkeypatch.setattr(relay_main, "POST_TIMEOUT", 2.0)
    monkeypatch.setattr(relay_main, "RETRY_DELAY", 0.5)
    monkeypatch.setattr(relay_main, "RETRY_MAX_DELAY", 10.0)
    monkeypatch.setattr(relay_main, "RETRY_MAX_ATTEMPTS", 5)

    cfg = RelayConfig.from_env()

    assert cfg == RelayConfig(
        host="10.0.0.5",
        port=12345,
        feed_read_timeout=15.0,
        server_url="https://example.com",
        relay_secret="s3cr3t",
        post_timeout=2.0,
        retry_delay=0.5,
        retry_max_delay=10.0,
        retry_max_attempts=5,
    )


@pytest.fixture
def fake_main(monkeypatch):
    """Replace relay.main.main with a coroutine that waits until cancelled,
    recording start/cancel events with the config it was given."""
    events = []

    async def _fake_main(config):
        events.append(("start", config))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append(("cancelled", config))
            raise

    monkeypatch.setattr(relay_main, "main", _fake_main)
    return events


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_double_start_raises_runtimeerror(fake_main):
    runner = RelayRunner()
    runner.start(_config())
    try:
        with pytest.raises(RuntimeError):
            runner.start(_config())
    finally:
        runner.stop()


def test_restart_before_start_raises_runtimeerror():
    runner = RelayRunner()
    with pytest.raises(RuntimeError):
        runner.restart(_config())


def test_run_coroutine_before_start_raises_runtimeerror():
    runner = RelayRunner()
    coro = asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError):
            runner.run_coroutine(coro)
    finally:
        coro.close()


def test_run_coroutine_executes_on_runner_loop(fake_main):
    runner = RelayRunner()
    runner.start(_config())
    try:
        future = runner.run_coroutine(asyncio.sleep(0, result="ok"))
        assert future.result(timeout=2.0) == "ok"
    finally:
        runner.stop()


def test_restart_cancels_previous_task_and_runs_new_one(fake_main):
    runner = RelayRunner()
    first = _config(host="1.1.1.1")
    second = _config(host="2.2.2.2")
    runner.start(first)
    runner.restart(second)
    try:
        assert _wait_until(lambda: len(fake_main) >= 3)
        # Snapshot before stop() (which cancels the still-running second
        # task and appends its own "cancelled" event).
        kinds = [kind for kind, _ in fake_main[:3]]
        hosts = [config.host for _, config in fake_main[:3]]
    finally:
        runner.stop()

    assert kinds == ["start", "cancelled", "start"]
    assert hosts[0] == "1.1.1.1"
    assert hosts[2] == "2.2.2.2"


def test_stop_cancels_running_task_and_joins_thread(fake_main):
    runner = RelayRunner()
    runner.start(_config())

    runner.stop()

    assert runner._thread is None
    assert any(kind == "cancelled" for kind, _ in fake_main)
