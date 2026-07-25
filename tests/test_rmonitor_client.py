"""Tests for RMonitorClient's idle-read timeout.

A stalled TCP connection (e.g. dropped by a NAT/firewall without a FIN/RST)
leaves `readline()` blocked forever with no exception raised, so the relay
never notices the feed died and never reconnects. `_read_loop` must give up
and return after `read_timeout` seconds of silence so `run()`'s reconnect
loop can recover.
"""

import asyncio

import pytest

from relay.rmonitor_client import RMonitorClient


class HangingReader:
    """Mimics a StreamReader on a stalled connection: readline() never returns."""

    async def readline(self):
        await asyncio.Event().wait()


class QueuedReader:
    """Returns queued lines, then hangs forever like HangingReader."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_read_loop_returns_after_idle_timeout():
    messages = []
    client = RMonitorClient("host", 1234, messages.append, read_timeout=0.05)
    client._reader = HangingReader()

    await asyncio.wait_for(client._read_loop(), timeout=1.0)

    assert messages == []


@pytest.mark.asyncio
async def test_read_loop_processes_messages_before_going_idle():
    messages = []
    client = RMonitorClient("host", 1234, messages.append, read_timeout=0.05)
    client._reader = QueuedReader([b'$B,31,"Practice"\r\n'])

    await asyncio.wait_for(client._read_loop(), timeout=1.0)

    assert messages == [{"type": "run", "unique_number": "31", "description": "Practice"}]


@pytest.mark.asyncio
async def test_read_loop_returns_immediately_on_remote_close():
    messages = []
    client = RMonitorClient("host", 1234, messages.append, read_timeout=5.0)
    client._reader = QueuedReader([b""])

    await asyncio.wait_for(client._read_loop(), timeout=1.0)

    assert messages == []


@pytest.mark.asyncio
async def test_on_connect_fires_once_after_successful_connect(monkeypatch):
    from relay import rmonitor_client

    connects = []

    async def fake_open_connection(host, port):
        return HangingReader(), None

    monkeypatch.setattr(rmonitor_client.asyncio, "open_connection", fake_open_connection)

    client = RMonitorClient(
        "host", 1234, lambda msg: None, on_connect=lambda: connects.append(1)
    )
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.wait_for(asyncio.sleep(0.05), timeout=1.0)
        assert connects == [1]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_on_disconnect_fires_once_when_read_loop_returns_on_idle_timeout(monkeypatch):
    from relay import rmonitor_client

    disconnects = []

    async def fake_open_connection(host, port):
        return HangingReader(), None

    monkeypatch.setattr(rmonitor_client.asyncio, "open_connection", fake_open_connection)

    client = RMonitorClient(
        "host", 1234, lambda msg: None, read_timeout=0.05,
        on_disconnect=lambda: disconnects.append(1),
    )
    task = asyncio.ensure_future(client.run(reconnect_delay=100))
    try:
        await asyncio.wait_for(asyncio.sleep(0.15), timeout=2.0)
        assert disconnects == [1]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_on_disconnect_fires_once_per_remote_close(monkeypatch):
    from relay import rmonitor_client

    disconnects = []

    async def fake_open_connection(host, port):
        return QueuedReader([b""]), None

    monkeypatch.setattr(rmonitor_client.asyncio, "open_connection", fake_open_connection)

    client = RMonitorClient(
        "host", 1234, lambda msg: None,
        on_disconnect=lambda: disconnects.append(1),
    )
    task = asyncio.ensure_future(client.run(reconnect_delay=100))
    try:
        await asyncio.wait_for(asyncio.sleep(0.05), timeout=2.0)
        assert disconnects == [1]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_on_disconnect_fires_once_on_cancellation(monkeypatch):
    from relay import rmonitor_client

    disconnects = []

    async def fake_open_connection(host, port):
        return HangingReader(), None

    monkeypatch.setattr(rmonitor_client.asyncio, "open_connection", fake_open_connection)

    client = RMonitorClient(
        "host", 1234, lambda msg: None,
        on_disconnect=lambda: disconnects.append(1),
    )
    task = asyncio.ensure_future(client.run())
    await asyncio.wait_for(asyncio.sleep(0.05), timeout=1.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert disconnects == [1]


@pytest.mark.asyncio
async def test_on_raw_line_fires_once_per_raw_line_including_unrecognized():
    messages = []
    raw_line_count = [0]
    client = RMonitorClient(
        "host", 1234, messages.append, read_timeout=0.05,
        on_raw_line=lambda: raw_line_count.__setitem__(0, raw_line_count[0] + 1),
    )
    client._reader = QueuedReader([b"$UNKNOWN,1\r\n", b'$B,31,"Practice"\r\n'])

    await asyncio.wait_for(client._read_loop(), timeout=1.0)

    assert raw_line_count[0] == 2
    assert messages == [{"type": "run", "unique_number": "31", "description": "Practice"}]
