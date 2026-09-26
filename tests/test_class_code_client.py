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
    assert len(ccc.RECORD_3) == 63
    assert len(ccc.RECORD_4) == 130


def test_every_identity_and_session_slot_in_the_constants_is_zero():
    r0 = ccc.RECORD_0
    assert r0[ccc.RECORD_0_TOKEN:ccc.RECORD_0_TOKEN + 6] == bytes(6)
    assert r0[ccc.RECORD_0_MACHINE:ccc.RECORD_0_MACHINE + ccc.MACHINE_SLOT_LEN] == bytes(64)
    for rec, anchors, tokens in (
        (ccc.RECORD_1B, ccc.RECORD_1B_ANCHORS, ccc.RECORD_1B_TOKENS),
        (ccc.RECORD_2B, ccc.RECORD_2B_ANCHORS, ccc.RECORD_2B_TOKENS),
        (ccc.RECORD_3, ccc.RECORD_3_ANCHORS, ccc.RECORD_3_TOKENS),
        (ccc.RECORD_4, ccc.RECORD_4_ANCHORS, ccc.RECORD_4_TOKENS),
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
        (ccc.RECORD_3, ccc.RECORD_3_ANCHORS, ccc.RECORD_3_TOKENS, 1, 1),
        (ccc.RECORD_4, ccc.RECORD_4_ANCHORS, ccc.RECORD_4_TOKENS, 2, 2),
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


def _s(text) -> bytes:
    """A length-prefixed registry string."""
    raw = text.encode() if isinstance(text, str) else text
    return struct.pack("<I", len(raw)) + raw


def _registry_record(reg_id="1a2b3c4d", tx=1234567, *, model="Test Car", capacity="2000",
                     code="TS", extras=("",) * 6, number="7",
                     class_name="Test Saloon Cup") -> bytes:
    """One synthetic registry record, followed by an invented person block."""
    return (
        _s(reg_id) + struct.pack("<III", 0, 1, tx) + bytes(8) + struct.pack("<I", 10)
        + _s(model) + _s(capacity) + _s(code) + b"".join(_s(e) for e in extras)
        + bytes(5) + _s(number) + _s(class_name)
        + _s("Ann") + _s("EXAMPLE") + bytes(12)
    )


def _triple(tx="1234567", class_name="Test Saloon Cup", code="TS"):
    return {"transponder": tx, "class_name": class_name, "class_code": code}


def test_registry_record_is_parsed():
    buf = bytes(16) + _registry_record() + bytes(16)
    assert ccc.parse_registry(buf) == [_triple()]


def test_registry_ids_shorter_than_eight_characters_are_parsed():
    buf = b"".join(
        _registry_record(reg_id, tx)
        for reg_id, tx in (("a", 11), ("1b2", 22), ("abcdef0", 33))
    )
    assert [r["transponder"] for r in ccc.parse_registry(buf)] == ["11", "22", "33"]


def test_registry_strings_between_code_and_number_are_skipped():
    rec = _registry_record(
        capacity="Example Racing Team",
        extras=("", "", "", "", "Example Racing", "1:23.456"),
        class_name="Test Sports Trophy", code="TT LT",
    )
    assert ccc.parse_registry(rec) == [_triple(class_name="Test Sports Trophy", code="TT LT")]


def test_registry_skips_codeless_classless_and_zero_transponder_records():
    buf = (
        _registry_record(tx=11, code="")
        + _registry_record(tx=22, class_name="")
        + _registry_record(tx=0)
        + _registry_record(tx=44)
    )
    assert ccc.parse_registry(buf) == [_triple(tx="44")]


def test_registry_triples_are_deduplicated():
    buf = (
        _registry_record("aaaa1111")
        + _registry_record("bbbb2222")
        + _registry_record("cccc3333", code="TX")
    )
    assert ccc.parse_registry(buf) == [_triple(), _triple(code="TX")]


def test_registry_skips_a_truncated_record_and_binary_noise():
    good = _registry_record(tx=11)
    # An id whose length byte disagrees with it, a string far too long to be
    # real, and a record cut off at the end of the buffer.
    mismatched = b"\x05\x00\x00\x00abc" + bytes(4) + struct.pack("<I", 1)
    oversized = _registry_record(tx=22, model="x" * 300)
    truncated = _registry_record(tx=33)[:40]
    buf = bytes(range(256)) + mismatched + good + oversized + truncated
    assert ccc.parse_registry(buf) == [_triple(tx="11")]


def test_registry_decodes_utf8_like_the_rmonitor_feed():
    raw_name = "Zoë Cup".encode() + b"\xff"
    (rec,) = ccc.parse_registry(_registry_record(class_name=raw_name))
    assert rec["class_name"] == raw_name.decode("utf-8", errors="replace")
    assert rec["class_name"] == "Zoë Cup�"


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
    first_idle=0.01, record_idle=0.01, model_idle=0.01, flush_quiet=0.05,
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


def _ages(msg):
    """Return every entry's ``age_seconds``, checking each is a sane number."""
    ages = [e["age_seconds"] for e in msg["entries"]]
    assert all(isinstance(a, float) and a >= 0 for a in ages)
    return ages


def _without_age(msg):
    """Return *msg* with each entry's ``age_seconds`` checked and removed."""
    _ages(msg)
    return {**msg, "entries": [
        {k: v for k, v in e.items() if k != "age_seconds"} for e in msg["entries"]
    ]}


def _collect(batches):
    async def on_batch(msg):
        # A preload is dated as a whole, not per entry.
        batches.append(msg if msg["type"] == "class_code_preload" else _without_age(msg))
    return on_batch


@pytest.mark.asyncio
async def test_handshake_sends_five_records_with_the_live_handle_and_unit(monkeypatch):
    writer = FakeWriter()
    _harness(monkeypatch, [(ChunkReader([IDENT_FRAME]), writer)])
    client = ccc.ClassCodeClient("timing-host", _collect([]), **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(writer.writes) >= 5)
        await asyncio.sleep(0.05)
    finally:
        await _finish(task)
    assert writer.writes == [
        ccc.build_record_0(token=TOKEN, machine=MACHINE),
        _session(ccc.RECORD_1B, ccc.RECORD_1B_ANCHORS, ccc.RECORD_1B_TOKENS),
        _session(ccc.RECORD_2B, ccc.RECORD_2B_ANCHORS, ccc.RECORD_2B_TOKENS),
        _session(ccc.RECORD_3, ccc.RECORD_3_ANCHORS, ccc.RECORD_3_TOKENS),
        _session(ccc.RECORD_4, ccc.RECORD_4_ANCHORS, ccc.RECORD_4_TOKENS),
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
        await _until(lambda: len(writer.writes) >= 7)
    finally:
        await _finish(task)
    keepalive = _session(ccc.KEEPALIVE, ccc.KEEPALIVE_ANCHORS, ccc.KEEPALIVE_TOKENS)
    assert writer.writes[5] == keepalive
    assert writer.writes[6] == keepalive


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
    assert len(writer.writes) == 5


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
    await _until(lambda: len(survivor.writes) >= 6)  # five records + a keepalive
    gate.set()
    with pytest.raises(_Stop):
        await asyncio.wait_for(task, timeout=2.0)
    assert sleeps == [60, 120, 60]


def _failing_client(monkeypatch, fail_calls, **knobs):
    """A client on one held connection whose *on_batch* fails on the call
    numbers in *fail_calls*; return ``(client, reader, calls, opened, sleeps)``.

    A short keepalive makes the idle hold re-poll the reader, so a chunk a
    test queues later is picked up.
    """
    gate = asyncio.Event()
    reader = ChunkReader([IDENT_FRAME, gate, _push("added", "0x4000AAAA", _fields("e1"))])
    opened, sleeps = _harness(monkeypatch, [(reader, FakeWriter())])
    calls = []

    async def on_batch(msg):
        calls.append((asyncio.get_running_loop().time(), _without_age(msg), _ages(msg)))
        if len(calls) in fail_calls:
            raise ConnectionError("server unreachable")

    client = ccc.ClassCodeClient(
        "timing-host", on_batch, **{**FAST, "keepalive_interval": 0.05, **knobs}
    )
    gate.set()
    return client, reader, calls, opened, sleeps


@pytest.mark.asyncio
async def test_a_failed_batch_is_kept_and_retried_on_the_same_connection(monkeypatch):
    client, _, calls, opened, sleeps = _failing_client(
        monkeypatch, {1}, retry_initial=0.2
    )
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(calls) >= 2)
    finally:
        await _finish(task)
    (t1, first, (age1,)), (t2, retried, (age2,)) = calls[:2]
    assert retried == first
    assert retried["entries"] == [_entry("added")]
    assert t2 - t1 >= 0.2
    # The retry is dated from the push, not re-dated at the retry.
    assert age2 - age1 >= 0.19
    assert len(opened) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_entries_pushed_after_a_failure_win_over_the_kept_ones(monkeypatch):
    client, reader, calls, _, _ = _failing_client(monkeypatch, {1}, retry_initial=0.3)
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: calls)
        reader._items.append(_push("modified", "0x4000AAAA", _fields("e1", number="17")))
        await _until(lambda: len(calls) >= 2)
    finally:
        await _finish(task)
    assert calls[1][1]["entries"] == [_entry("modified", number="17")]


@pytest.mark.asyncio
async def test_retry_delay_doubles_while_delivery_fails_and_resets_on_success(monkeypatch):
    client, _, calls, _, _ = _failing_client(
        monkeypatch, {1, 2, 3}, retry_initial=0.02, retry_max=0.08
    )
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(calls) >= 3)
        assert client._retry_delay == 0.08
        await _until(lambda: len(calls) >= 4)
        await asyncio.sleep(0.05)
    finally:
        await _finish(task)
    assert len(calls) == 4
    assert client._retry_delay == client.retry_initial


@pytest.mark.asyncio
async def test_undelivered_entries_are_dropped_once_older_than_the_server_would_keep(monkeypatch):
    """A batch the server keeps rejecting is not retried for ever."""
    monkeypatch.setattr(ccc, "MAX_ENTRY_AGE", 0.3)
    client, _, calls, opened, _ = _failing_client(
        monkeypatch, set(range(1, 100)), retry_initial=0.03, retry_max=0.03
    )
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: calls)
        await asyncio.sleep(0.6)
        tried = len(calls)
        await asyncio.sleep(0.2)
    finally:
        await _finish(task)
    assert tried >= 2
    assert len(calls) == tried  # no attempts once the entry aged out
    assert client._pending == {}
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_a_slow_delivery_does_not_hold_up_the_keepalive(monkeypatch):
    """``on_batch`` can spend minutes in HTTP retries; the socket must stay alive."""
    gate = asyncio.Event()
    release = asyncio.Event()
    reader = ChunkReader([IDENT_FRAME, gate, _push("added", "0x4000AAAA", _fields())])
    writer = FakeWriter()
    opened, sleeps = _harness(monkeypatch, [(reader, writer)])
    started = []

    async def on_batch(msg):
        started.append(msg)
        await release.wait()

    client = ccc.ClassCodeClient(
        "timing-host", on_batch, **{**FAST, "keepalive_interval": 0.05}
    )
    task = asyncio.ensure_future(client.run())
    try:
        gate.set()
        await _until(lambda: started)
        sent_at_start = len(writer.writes)
        await asyncio.sleep(0.3)
        keepalives = len(writer.writes) - sent_at_start
        # Records pushed during the slow delivery wait for it, not beside it.
        reader._items.append(_push("modified", "0x4000AAAA", _fields("e2")))
        await asyncio.sleep(0.2)
        assert len(started) == 1
        release.set()
        await _until(lambda: len(started) >= 2)
    finally:
        await _finish(task)
    assert keepalives >= 3
    assert len(opened) == 1
    assert sleeps == []
    assert [e["entrant_id"] for e in started[1]["entries"]] == ["e2"]


@pytest.mark.asyncio
async def test_a_delivery_failing_as_the_connection_closes_waits_for_its_retry(monkeypatch):
    """The disconnect path honours the delivery backoff instead of re-sending at once."""
    gate = asyncio.Event()
    closing = asyncio.Event()
    reader = ChunkReader([
        IDENT_FRAME, gate, _push("added", "0x4000AAAA", _fields()), closing, b"",
    ])
    _, sleeps = _harness(monkeypatch, [(reader, FakeWriter())])
    calls = []

    async def on_batch(msg):
        calls.append(msg)
        closing.set()  # the connection drops while this delivery is failing
        await asyncio.sleep(0.05)
        raise ConnectionError("server unreachable")

    client = ccc.ClassCodeClient("timing-host", on_batch, **{**FAST, "retry_initial": 10.0})
    task = asyncio.ensure_future(client.run())
    gate.set()
    with pytest.raises(_Stop):
        await asyncio.wait_for(task, timeout=2.0)
    assert len(calls) == 1
    assert sleeps == [client.reconnect_initial]
    assert [list(v) for v in client._pending.values()] == [["e1"]]  # kept for later


@pytest.mark.asyncio
async def test_unexpected_exception_is_logged_and_retried_not_raised(monkeypatch, caplog):
    opened, sleeps = _harness(monkeypatch, [RuntimeError("boom")])
    client = ccc.ClassCodeClient("timing-host", _collect([]), **FAST)
    with pytest.raises(_Stop):
        await asyncio.wait_for(client.run(), timeout=2.0)
    assert sleeps == [client.reconnect_initial]
    assert "unexpected error" in caplog.text


class HostReader:
    """A host that answers the records the client writes.

    *replies* maps a record count to the bytes the host sends once the client
    has written that many records — record 0 is the first write, record 3
    the fourth.  Once every reply has gone, *then* is served as by
    :class:`ChunkReader`, except that a float in it is a pause of that many
    seconds; after that the stream hangs.
    """

    def __init__(self, writer, replies, then=()):
        self._writer = writer
        self._replies = dict(replies)
        self._then = list(then)

    async def read(self, n):
        while self._replies:
            due = min(self._replies)
            if len(self._writer.writes) >= due:
                return self._replies.pop(due)
            await asyncio.sleep(0.002)
        while self._then:
            item = self._then[0]
            if isinstance(item, float):
                await asyncio.sleep(item)
            elif isinstance(item, asyncio.Event):
                await item.wait()
            else:
                return self._then.pop(0)
            # Removed only once waited out, as ChunkReader does.
            self._then.pop(0)
        await asyncio.Event().wait()


REGISTRY = _registry_record("aaaa1111", 11) + _registry_record(
    "bbbb2222", 22, class_name="Test Sports Trophy", code="TT"
)
REGISTRY_ENTRIES = [_triple("11"), _triple("22", "Test Sports Trophy", "TT")]


def _pulling(writer, registry=REGISTRY, then=()):
    """A host that sends *registry* split across its answers to records 3 and 4."""
    half = len(registry) // 2
    return HostReader(writer, {1: IDENT_FRAME, 4: registry[:half], 5: registry[half:]}, then)


def _preloads(batches):
    return [b for b in batches if b["type"] == "class_code_preload"]


@pytest.mark.asyncio
async def test_a_complete_registry_pull_is_forwarded_as_one_preload(monkeypatch):
    writer = FakeWriter()
    opened, sleeps = _harness(monkeypatch, [(_pulling(writer), writer)])
    batches = []

    async def on_batch(msg):
        batches.append(msg)

    client = ccc.ClassCodeClient("timing-host", on_batch, **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: batches)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    (msg,) = batches
    assert msg["type"] == "class_code_preload"
    assert msg["entries"] == REGISTRY_ENTRIES
    assert isinstance(msg["age_seconds"], float) and msg["age_seconds"] >= 0
    assert len(opened) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_a_pull_with_no_registry_records_forwards_nothing(monkeypatch, caplog):
    writer = FakeWriter()
    _harness(monkeypatch, [(_pulling(writer, registry=bytes(range(200))), writer)])
    batches = []
    client = ccc.ClassCodeClient("timing-host", _collect(batches), **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(writer.writes) >= 5)
        await asyncio.sleep(0.2)
    finally:
        await _finish(task)
    assert batches == []
    assert client._preload is None
    assert "held no usable records" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("cut_short_by", ["model_cap", "buffer_cap"])
async def test_an_incomplete_registry_pull_keeps_the_connection_and_sends_no_preload(
    monkeypatch, caplog, cut_short_by
):
    """A partial registry would let the server's class-uniform guard pass a
    code that a missing record contradicts, so it is never forwarded."""
    writer = FakeWriter()
    if cut_short_by == "model_cap":
        # The model keeps coming, never idling, past the cap.
        # Gaps a tenth of model_idle, so a scheduler stall can't idle it early.
        reader = HostReader(writer, {1: IDENT_FRAME, 4: REGISTRY}, then=[0.02, b"\x00"] * 50)
        knobs = {"model_idle": 0.2, "model_cap": 0.4}
    else:
        monkeypatch.setattr(ccc, "MODEL_BUFFER_CAP", len(REGISTRY) - 1)
        reader = _pulling(writer)
        knobs = {}
    opened, sleeps = _harness(monkeypatch, [(reader, writer)])
    batches = []
    client = ccc.ClassCodeClient(
        "timing-host", _collect(batches), **{**FAST, "keepalive_interval": 0.1, **knobs}
    )
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(1.4)
    finally:
        await _finish(task)
    assert _preloads(batches) == []
    assert "registry pull incomplete" in caplog.text
    keepalive = _session(ccc.KEEPALIVE, ccc.KEEPALIVE_ANCHORS, ccc.KEEPALIVE_TOKENS)
    assert keepalive in writer.writes  # still held
    assert len(opened) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_a_failed_preload_is_retried_and_a_newer_pull_replaces_it(monkeypatch):
    closing = asyncio.Event()
    first, second = FakeWriter(), FakeWriter()
    newer = _registry_record("cccc3333", 33, code="TN")
    connections = [
        (_pulling(first, then=[closing, b""]), first),
        (_pulling(second, registry=newer), second),
    ]
    opened, sleeps = _harness(monkeypatch, connections, stop_after=2)
    calls = []

    async def on_batch(msg):
        calls.append(msg["entries"])
        if len(calls) == 2:
            closing.set()
        if len(calls) <= 2:
            raise ConnectionError("server unreachable")

    client = ccc.ClassCodeClient("timing-host", on_batch, **{**FAST, "retry_initial": 0.05})
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(calls) >= 3)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    # Retried once on the first connection; the second connection's pull then
    # replaced the kept preload rather than queueing behind it.
    assert calls == [REGISTRY_ENTRIES, REGISTRY_ENTRIES, [_triple("33", code="TN")]]
    assert len(opened) == 2
    assert client._preload is None


@pytest.mark.asyncio
async def test_a_preload_older_than_the_server_would_keep_is_dropped(monkeypatch, caplog):
    monkeypatch.setattr(ccc, "MAX_ENTRY_AGE", 0.15)
    writer = FakeWriter()
    _harness(monkeypatch, [(_pulling(writer), writer)])
    calls = []

    async def on_batch(msg):
        calls.append(msg)
        raise ConnectionError("server unreachable")

    client = ccc.ClassCodeClient(
        "timing-host", on_batch,
        **{**FAST, "keepalive_interval": 0.05, "retry_initial": 0.03, "retry_max": 0.03},
    )
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: calls)
        await asyncio.sleep(0.4)
        tried = len(calls)
        await asyncio.sleep(0.2)
    finally:
        await _finish(task)
    assert tried >= 2
    assert len(calls) == tried
    assert client._preload is None
    assert "Dropping an undelivered class-code preload" in caplog.text


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _statuses():
    seen = []
    return seen, seen.append


@pytest.mark.asyncio
async def test_status_reports_connecting_then_connected(monkeypatch):
    writer = FakeWriter()
    _harness(monkeypatch, [(HostReader(writer, {1: IDENT_FRAME}), writer)])
    seen, on_status = _statuses()
    client = ccc.ClassCodeClient("timing-host", _collect([]), on_status=on_status, **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(seen) >= 2)
        await asyncio.sleep(0.05)
    finally:
        await _finish(task)
    assert [s.state for s in seen] == ["connecting", "connected"]
    assert seen[-1] == ccc.ClassCodeStatus("connected")


@pytest.mark.asyncio
async def test_status_reports_a_failed_connection_with_its_retry_delay_and_reason(monkeypatch):
    _harness(monkeypatch, [ConnectionRefusedError("connection refused")])
    seen, on_status = _statuses()
    client = ccc.ClassCodeClient("timing-host", _collect([]), on_status=on_status, **FAST)
    with pytest.raises(_Stop):
        await asyncio.wait_for(client.run(), timeout=2.0)
    assert [s.state for s in seen] == ["connecting", "retrying"]
    assert seen[-1].retry_in == client.reconnect_initial
    assert seen[-1].detail == "connection refused"


@pytest.mark.asyncio
async def test_status_counts_preloaded_and_pushed_deliveries(monkeypatch):
    gate = asyncio.Event()
    writer = FakeWriter()
    push = _push("added", "0x4000AAAA", _fields())
    _harness(monkeypatch, [(_pulling(writer, then=[gate, push]), writer)])
    seen, on_status = _statuses()
    batches = []
    client = ccc.ClassCodeClient(
        "timing-host", _collect(batches), on_status=on_status,
        **{**FAST, "keepalive_interval": 0.05},
    )
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: batches)
        after_preload = seen[-1]
        gate.set()
        await _until(lambda: len(batches) >= 2)
        await asyncio.sleep(0.05)
    finally:
        await _finish(task)
    assert after_preload.state == "connected"
    assert (after_preload.preloaded, after_preload.pushed) == (2, 0)
    assert after_preload.last_delivery is not None
    assert (seen[-1].preloaded, seen[-1].pushed) == (2, 1)
    assert seen[-1].last_delivery >= after_preload.last_delivery


@pytest.mark.asyncio
async def test_status_reports_a_failed_delivery_with_its_retry_delay(monkeypatch):
    client, _, calls, _, _ = _failing_client(monkeypatch, {1}, retry_initial=0.2)
    seen, client.on_status = _statuses()
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(calls) >= 2)
        await asyncio.sleep(0.02)
    finally:
        await _finish(task)
    failed = [s for s in seen if s.state == "delivery_failed"]
    assert [s.retry_in for s in failed] == [0.2]
    assert seen[-1].state == "connected"
    assert seen[-1].pushed == 1


@pytest.mark.asyncio
async def test_a_raising_status_callback_does_not_stop_the_client(monkeypatch, caplog):
    writer = FakeWriter()
    opened, sleeps = _harness(monkeypatch, [(_pulling(writer), writer)])

    def on_status(status):
        raise RuntimeError("display gone")

    batches = []
    client = ccc.ClassCodeClient("timing-host", _collect(batches), on_status=on_status, **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: batches)
    finally:
        await _finish(task)
    assert _preloads(batches)
    assert len(opened) == 1
    assert sleeps == []
    assert "status callback failed" in caplog.text
