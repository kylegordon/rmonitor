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
