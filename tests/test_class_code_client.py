"""Tests for the ``:51738`` class-code client.

Every byte here is synthetic: the handshake constants are checked for being
scrubbed, and push records are built in-test from invented names, numbers and
codes — never from a capture.
"""

import asyncio
import struct

import pytest

from relay import class_code_client as ccc


TOKEN = bytes.fromhex("0a0b0c0d0e0f")
MACHINE = b"RELAY-0E0F"
UNIT = bytes.fromhex("112233445566")
HANDLE = b"\x7a\x7b"
IDENT_FRAME = b"\x00\x00\x01\x04" + bytes(5) + UNIT + bytes(2) + HANDLE + bytes(10)


def _fields(entrant="e1", number="7", class_name="Test Saloon Cup", code="TS", tx="1234567"):
    return [
        entrant, entrant + "b", number, class_name, tx, "0", tx,
        "Ann", "EXAMPLE", "Test Car", "2000", code,
    ] + [""] * 7


def _push(kind, run_id, fields) -> bytes:
    body = f"Competitor {kind} [{run_id}]:".encode() + "\t".join(fields).encode()
    return b"datamanager" + struct.pack("<I", len(body)) + body


def _entry(kind="added", entrant="e1", number="7", class_name="Test Saloon Cup",
           code="TS", tx="1234567"):
    return {
        "entrant_id": entrant, "kind": kind, "number": number,
        "class_name": class_name, "transponder": tx, "class_code": code,
    }


def _session(template, anchors, tokens, *, handle=HANDLE, unit=UNIT, token=TOKEN):
    return ccc.build_session_record(
        template, token=token, handle=handle, unit=unit, anchors=anchors, tokens=tokens
    )


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def test_constants_have_the_captured_lengths():
    assert len(ccc.RECORD_0) == 85
    assert len(ccc.RECORD_1B) == 249
    assert len(ccc.RECORD_2B) == 185
    assert len(ccc.KEEPALIVE) == 63


def test_every_identity_and_session_slot_in_the_constants_is_zero():
    r0 = ccc.RECORD_0
    assert r0[ccc.RECORD_0_TOKEN:ccc.RECORD_0_TOKEN + 6] == bytes(6)
    assert r0[ccc.RECORD_0_MACHINE:ccc.RECORD_0_MACHINE + ccc.MACHINE_SLOT_LEN] == bytes(64)
    for rec, anchors, tokens in (
        (ccc.RECORD_1B, ccc.RECORD_1B_ANCHORS, ccc.RECORD_1B_TOKENS),
        (ccc.RECORD_2B, ccc.RECORD_2B_ANCHORS, ccc.RECORD_2B_TOKENS),
        (ccc.KEEPALIVE, ccc.KEEPALIVE_ANCHORS, ccc.KEEPALIVE_TOKENS),
    ):
        for a in anchors:
            assert rec[a:a + 10] == bytes(10)
        for t in tokens:
            assert rec[t:t + 6] == bytes(6)
    for rec in (ccc.RECORD_1B, ccc.RECORD_2B):
        assert rec[ccc.IP_SLOT:ccc.IP_SLOT + 4] == bytes(4)


def test_record_0_carries_token_and_nul_padded_machine_name():
    rec = ccc.build_record_0(token=TOKEN, machine=MACHINE)
    assert len(rec) == 85
    assert rec[9:15] == TOKEN
    assert rec[21:85] == MACHINE + bytes(64 - len(MACHINE))
    assert rec[:9] == ccc.RECORD_0[:9]
    assert rec[15:21] == ccc.RECORD_0[15:21]


def test_machine_name_over_64_bytes_is_rejected():
    with pytest.raises(ValueError):
        ccc.build_record_0(token=TOKEN, machine=b"X" * 65)
    with pytest.raises(ValueError):
        ccc.build_record_0(token=b"short", machine=MACHINE)


def test_session_records_substitute_handle_and_unit_at_every_anchor():
    handle = b"\xab\xcd"
    unit = bytes(range(1, 7))
    anchor = handle + b"\0\0" + unit
    for template, anchors, tokens, n_anchor, n_token in (
        (ccc.RECORD_1B, ccc.RECORD_1B_ANCHORS, ccc.RECORD_1B_TOKENS, 1, 2),
        (ccc.RECORD_2B, ccc.RECORD_2B_ANCHORS, ccc.RECORD_2B_TOKENS, 2, 3),
        (ccc.KEEPALIVE, ccc.KEEPALIVE_ANCHORS, ccc.KEEPALIVE_TOKENS, 1, 1),
    ):
        rec = _session(template, anchors, tokens, handle=handle, unit=unit)
        assert len(rec) == len(template)
        assert rec.count(anchor) == n_anchor
        assert rec.count(TOKEN) == n_token
    with pytest.raises(ValueError):
        _session(ccc.KEEPALIVE, (4,), (22,), handle=b"\x01")
    with pytest.raises(ValueError):
        _session(ccc.KEEPALIVE, (4,), (22,), unit=b"\x01")


def test_relay_identity_uses_getnode(monkeypatch):
    monkeypatch.setattr(ccc.uuid, "getnode", lambda: 0x0123456789AB)
    ccc.relay_identity.cache_clear()
    try:
        assert ccc.relay_identity() == (bytes.fromhex("0123456789ab"), b"RELAY-89AB")
    finally:
        ccc.relay_identity.cache_clear()


def test_identity_frame_yields_unit_and_handle():
    assert ccc.parse_identity_frame(IDENT_FRAME) == (UNIT, HANDLE)


def test_non_identity_bytes_yield_none():
    assert ccc.parse_identity_frame(b"\x00\x02") is None
    assert ccc.parse_identity_frame(b"\x11\x10" + bytes(30)) is None
    assert ccc.parse_identity_frame(IDENT_FRAME[:18]) is None


def test_parser_reads_one_record():
    recs = ccc.PushParser().feed(_push("added", "0x4000AAAA", _fields()))
    assert recs == [ccc.PushRecord("added", "0x4000AAAA", _fields())]


def test_parser_reassembles_a_record_fed_one_byte_at_a_time():
    data = _push("automatically added", "0x4000AAAA", _fields())
    parser = ccc.PushParser()
    recs = []
    for i in range(len(data)):
        recs += parser.feed(data[i:i + 1])
    assert recs == [ccc.PushRecord("automatically added", "0x4000AAAA", _fields())]


def test_parser_reads_two_records_from_one_chunk():
    data = _push("added", "0x4000AAAA", _fields("e1")) + _push(
        "modified", "0x8000BBBB", _fields("e2")
    )
    recs = ccc.PushParser().feed(data)
    assert [(r.kind, r.run_id, r.fields[0]) for r in recs] == [
        ("added", "0x4000AAAA", "e1"),
        ("modified", "0x8000BBBB", "e2"),
    ]


def test_parser_skips_leading_binary_noise():
    parser = ccc.PushParser()
    # Noise long enough to be trimmed, a spurious marker with an absurd
    # length, then a real record.
    noise = bytes(range(256)) + b"datamanager" + struct.pack("<I", 0xFFFFFFFF)
    assert parser.feed(noise) == []
    recs = parser.feed(_push("added", "0x4000AAAA", _fields()))
    assert [r.fields[0] for r in recs] == ["e1"]


def test_parser_skips_a_non_competitor_body():
    body = b"Session loaded [0x4000AAAA]:nothing"
    data = b"datamanager" + struct.pack("<I", len(body)) + body
    parser = ccc.PushParser()
    assert parser.feed(data + _push("added", "0x4000AAAA", _fields())) == [
        ccc.PushRecord("added", "0x4000AAAA", _fields())
    ]


def test_record_entry_skips_short_or_codeless_records():
    assert ccc.record_entry(ccc.PushRecord("added", "r", _fields()[:11])) is None
    assert ccc.record_entry(ccc.PushRecord("added", "r", _fields(code=""))) is None
    assert ccc.record_entry(ccc.PushRecord("added", "r", _fields(entrant=""))) is None
    assert ccc.record_entry(ccc.PushRecord("modified", "r", _fields())) == _entry("modified")


def test_parser_decodes_utf8_like_the_rmonitor_feed():
    raw_name = "Zoë".encode() + b"\xff"
    fields = _fields()
    body = b"Competitor added [r]:" + b"\t".join(
        f.encode() if i != 7 else raw_name for i, f in enumerate(fields)
    )
    data = b"datamanager" + struct.pack("<I", len(body)) + body
    (rec,) = ccc.PushParser().feed(data)
    assert rec.fields[7] == raw_name.decode("utf-8", errors="replace")
    assert rec.fields[7] == "Zoë�"


# ---------------------------------------------------------------------------
# The connection loop
# ---------------------------------------------------------------------------

class FakeWriter:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    async def wait_closed(self):
        pass


class ChunkReader:
    """Returns queued chunks, then hangs forever.

    A queued ``asyncio.Event`` is waited on before the next chunk, so a test
    can hold the stream open; a queued ``b""`` is EOF.  An event is removed
    only once it is set: the client cancels an idle read, and a gate popped
    before that cancellation would be lost.
    """

    def __init__(self, items):
        self._items = list(items)

    async def read(self, n):
        while self._items:
            item = self._items[0]
            if isinstance(item, asyncio.Event):
                await item.wait()
                self._items.pop(0)
                continue
            return self._items.pop(0)
        await asyncio.Event().wait()


class _Stop(BaseException):
    """Raised from the fake backoff sleep to end ``run()``."""


FAST = dict(
    first_idle=0.01, record_idle=0.01, tail_idle=0.01, flush_quiet=0.05,
    keepalive_interval=10.0,
)


def _harness(monkeypatch, connections, *, stop_after=1):
    """Patch the network and backoff; return ``(opened, sleeps)``.

    *connections* is a list of ``(reader, writer)`` pairs or exceptions,
    handed out one per connect.
    """
    opened = []
    sleeps = []

    async def fake_open_connection(host, port):
        conn = connections[min(len(opened), len(connections) - 1)]
        opened.append((host, port))
        if isinstance(conn, BaseException):
            raise conn
        return conn

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= stop_after:
            raise _Stop()

    monkeypatch.setattr(ccc.asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(ccc, "_backoff_sleep", fake_sleep)
    monkeypatch.setattr(ccc, "relay_identity", lambda: (TOKEN, MACHINE))
    return opened, sleeps


async def _until(cond, timeout=2.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout=timeout)


async def _finish(task):
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, _Stop):
        pass


def _collect(batches):
    async def on_batch(msg):
        batches.append(msg)
    return on_batch


@pytest.mark.asyncio
async def test_handshake_sends_three_records_with_the_live_handle_and_unit(monkeypatch):
    writer = FakeWriter()
    _harness(monkeypatch, [(ChunkReader([IDENT_FRAME]), writer)])
    client = ccc.ClassCodeClient("timing-host", _collect([]), **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(writer.writes) >= 3)
        await asyncio.sleep(0.05)
    finally:
        await _finish(task)
    assert writer.writes == [
        ccc.build_record_0(token=TOKEN, machine=MACHINE),
        _session(ccc.RECORD_1B, ccc.RECORD_1B_ANCHORS, ccc.RECORD_1B_TOKENS),
        _session(ccc.RECORD_2B, ccc.RECORD_2B_ANCHORS, ccc.RECORD_2B_TOKENS),
    ]


@pytest.mark.asyncio
async def test_no_identity_frame_sends_only_record_0_then_backs_off(monkeypatch):
    writer = FakeWriter()
    opened, sleeps = _harness(monkeypatch, [(ChunkReader([b"\x00\x02"]), writer)])
    client = ccc.ClassCodeClient("timing-host", _collect([]), **FAST)
    with pytest.raises(_Stop):
        await asyncio.wait_for(client.run(), timeout=2.0)
    assert writer.writes == [ccc.build_record_0(token=TOKEN, machine=MACHINE)]
    assert writer.closed
    assert opened == [("timing-host", ccc.PORT)]
    assert sleeps == [client.reconnect_initial]


@pytest.mark.asyncio
async def test_keepalive_is_sent_with_the_live_session_fields(monkeypatch):
    writer = FakeWriter()
    _harness(monkeypatch, [(ChunkReader([IDENT_FRAME]), writer)])
    client = ccc.ClassCodeClient(
        "timing-host", _collect([]), **{**FAST, "keepalive_interval": 0.05}
    )
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(writer.writes) >= 5)
    finally:
        await _finish(task)
    keepalive = _session(ccc.KEEPALIVE, ccc.KEEPALIVE_ANCHORS, ccc.KEEPALIVE_TOKENS)
    assert writer.writes[3] == keepalive
    assert writer.writes[4] == keepalive


@pytest.mark.asyncio
async def test_silence_does_not_trigger_a_reconnect(monkeypatch):
    writer = FakeWriter()
    opened, sleeps = _harness(monkeypatch, [(ChunkReader([IDENT_FRAME]), writer)])
    client = ccc.ClassCodeClient("timing-host", _collect([]), **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(0.3)
    finally:
        await _finish(task)
    assert len(opened) == 1
    assert sleeps == []
    assert len(writer.writes) == 3


@pytest.mark.asyncio
async def test_a_burst_across_reads_is_one_batch_per_run_deduplicated_last_wins(monkeypatch):
    gate = asyncio.Event()
    first = _push("added", "0x4000AAAA", _fields(number="7"))
    again = _push("modified", "0x4000AAAA", _fields(number="17"))
    other = _push("added", "0x4000AAAA", _fields(entrant="e2", number="8", code="TX"))
    reader = ChunkReader([IDENT_FRAME, gate, first[:20], first[20:] + again[:5], again[5:] + other])
    _harness(monkeypatch, [(reader, FakeWriter())])
    batches = []
    client = ccc.ClassCodeClient("timing-host", _collect(batches), **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(0.1)
        gate.set()
        await _until(lambda: batches)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert batches == [{
        "type": "class_codes",
        "run_id": "0x4000AAAA",
        "entries": [
            _entry("modified", number="17"),
            _entry("added", entrant="e2", number="8", code="TX"),
        ],
    }]


@pytest.mark.asyncio
async def test_two_run_ids_in_one_burst_are_two_batches(monkeypatch):
    gate = asyncio.Event()
    data = _push("added", "0x4000AAAA", _fields()) + _push("modified", "0x8000BBBB", _fields())
    _harness(monkeypatch, [(ChunkReader([IDENT_FRAME, gate, data]), FakeWriter())])
    batches = []
    client = ccc.ClassCodeClient("timing-host", _collect(batches), **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(0.05)
        gate.set()
        await _until(lambda: len(batches) >= 2)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert [(b["run_id"], b["entries"]) for b in batches] == [
        ("0x4000AAAA", [_entry("added")]),
        ("0x8000BBBB", [_entry("modified")]),
    ]


@pytest.mark.asyncio
async def test_pending_entries_are_flushed_when_the_connection_closes(monkeypatch):
    gate = asyncio.Event()
    reader = ChunkReader([IDENT_FRAME, gate, _push("added", "0x4000AAAA", _fields()), b""])
    writer = FakeWriter()
    _, sleeps = _harness(monkeypatch, [(reader, writer)])
    batches = []
    client = ccc.ClassCodeClient(
        "timing-host", _collect(batches), **{**FAST, "flush_quiet": 10.0}
    )
    task = asyncio.ensure_future(client.run())
    await asyncio.sleep(0.05)
    gate.set()
    with pytest.raises(_Stop):
        await asyncio.wait_for(task, timeout=2.0)
    assert [b["entries"] for b in batches] == [[_entry("added")]]
    assert writer.closed
    assert sleeps == [client.reconnect_initial]


@pytest.mark.asyncio
async def test_reconnect_delay_doubles_to_the_cap(monkeypatch):
    # A fresh EOF-only reader per connect, so every handshake fails.
    connections = [(ChunkReader([b""]), FakeWriter()) for _ in range(6)]
    opened, sleeps = _harness(monkeypatch, connections, stop_after=6)
    client = ccc.ClassCodeClient("timing-host", _collect([]), **FAST)
    with pytest.raises(_Stop):
        await asyncio.wait_for(client.run(), timeout=2.0)
    assert sleeps == [60, 120, 240, 480, 900, 900]
    assert len(opened) == 6


@pytest.mark.asyncio
async def test_reconnect_delay_resets_after_a_connection_that_survived_a_keepalive(monkeypatch):
    gate = asyncio.Event()
    survivor = FakeWriter()
    connections = [
        (ChunkReader([b""]), FakeWriter()),
        (ChunkReader([b""]), FakeWriter()),
        (ChunkReader([IDENT_FRAME, gate, b""]), survivor),
    ]
    _, sleeps = _harness(monkeypatch, connections, stop_after=3)
    client = ccc.ClassCodeClient(
        "timing-host", _collect([]), **{**FAST, "keepalive_interval": 0.05}
    )
    task = asyncio.ensure_future(client.run())
    await _until(lambda: len(survivor.writes) >= 4)  # three records + a keepalive
    gate.set()
    with pytest.raises(_Stop):
        await asyncio.wait_for(task, timeout=2.0)
    assert sleeps == [60, 120, 60]


@pytest.mark.asyncio
async def test_a_failing_batch_callback_does_not_end_the_connection(monkeypatch):
    gate = asyncio.Event()
    reader = ChunkReader([
        IDENT_FRAME, gate, _push("added", "0x4000AAAA", _fields("e1")),
    ])
    writer = FakeWriter()
    opened, sleeps = _harness(monkeypatch, [(reader, writer)])
    calls = []

    async def on_batch(msg):
        calls.append(msg)
        if len(calls) == 1:
            raise RuntimeError("server rejected it")

    # A short keepalive makes the idle hold re-poll the reader, so a chunk
    # queued later is picked up.
    client = ccc.ClassCodeClient(
        "timing-host", on_batch, **{**FAST, "keepalive_interval": 0.05}
    )
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(0.05)
        gate.set()
        await _until(lambda: calls)
        # A later burst on the same connection is still delivered.
        reader._items.append(_push("modified", "0x4000AAAA", _fields("e2")))
        await _until(lambda: len(calls) >= 2)
    finally:
        await _finish(task)
    assert [c["entries"][0]["entrant_id"] for c in calls] == ["e1", "e2"]
    assert len(opened) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_unexpected_exception_is_logged_and_retried_not_raised(monkeypatch, caplog):
    opened, sleeps = _harness(monkeypatch, [RuntimeError("boom")])
    client = ccc.ClassCodeClient("timing-host", _collect([]), **FAST)
    with pytest.raises(_Stop):
        await asyncio.wait_for(client.run(), timeout=2.0)
    assert sleeps == [client.reconnect_initial]
    assert "unexpected error" in caplog.text
