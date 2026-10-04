"""Tests for the ``:51738`` class-code client.

Every byte here is synthetic: the handshake constants are checked for being
scrubbed, and push records are built in-test from invented names, numbers and
codes — never from a capture.  The one exception is the Announcements view:
its records and frames are captured layouts, kept byte for byte because the
layout is the thing under test, with every identity slot zeroed and only an
operator's test text in them.
"""

import asyncio
import json
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
    assert len(ccc.VIEW_HEADER) == 59


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
        (ccc.VIEW_HEADER, ccc.VIEW_ANCHORS, ccc.VIEW_TOKENS),
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


def _triple(tx="1234567", class_name="Test Saloon Cup", code="TS", *,
            reg_id="1a2b3c4d", number="7"):
    return {
        "registration_id": reg_id, "number": number,
        "transponder": tx, "class_name": class_name, "class_code": code,
    }


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


def test_registry_keeps_codeless_records_and_skips_classless_and_zero_transponder_ones():
    """A codeless record is kept: the server's class-uniform guard must see it."""
    buf = (
        _registry_record(tx=11, code="")
        + _registry_record(tx=22, class_name="")
        + _registry_record(tx=0)
        + _registry_record(tx=44)
    )
    assert ccc.parse_registry(buf) == [_triple(tx="11", code=""), _triple(tx="44")]


def test_registry_keeps_one_entry_per_registration():
    """Two registrations sharing a transponder, class and code are two
    entries; the server's number layer needs each one's id."""
    buf = (
        _registry_record("aaaa1111")
        + _registry_record("bbbb2222")
        + _registry_record("aaaa1111")
    )
    assert ccc.parse_registry(buf) == [
        _triple(reg_id="aaaa1111"), _triple(reg_id="bbbb2222"),
    ]


def test_registry_carries_registration_id_and_number():
    (rec,) = ccc.parse_registry(_registry_record("509ff32b", number="12X"))
    assert rec["registration_id"] == "509ff32b"
    assert rec["number"] == "12X"


def test_registry_parses_non_hex_registration_ids():
    long_id = "Ab" * 16
    buf = (
        _registry_record("Competitio1616", 11)
        + _registry_record(long_id, 22)
        + _registry_record(long_id + "c", 33)
    )
    assert [(r["registration_id"], r["transponder"]) for r in ccc.parse_registry(buf)] == [
        ("Competitio1616", "11"), (long_id, "22"),
    ]


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


def _run_record(run_id=0x40002806, group_id=0x80000985, name="Race 7 - 2nd Race",
                flags=0x23C) -> bytes:
    """One synthetic run-table record, followed by invented settings."""
    return (
        struct.pack("<I", 1) + struct.pack("<III", 0x6523A1B0, 0x6523A1B1, 0x6523A1B2)
        + struct.pack("<III", run_id, flags, group_id) + _s(name) + bytes(8)
    )


def _group_record(group_id=0x800009D4, parent=0x800009D2,
                  name="Scottish Championship Pre-Injection 600", *, pad=bytes(8)) -> bytes:
    """One group record laid out as captured, with invented timestamps."""
    return (
        struct.pack("<I", group_id) + b"\x00" + struct.pack("<I", 0x98)
        + struct.pack("<I", parent) + _s(name) + pad
        + struct.pack("<II", 0x6523A1B0, 0x6523A1B1) + b"\x05"
    )


def test_parse_groups_reads_a_group_record():
    buf = bytes(16) + _group_record() + bytes(16)
    assert ccc.parse_groups(buf) == {
        "0x800009D4": "Scottish Championship Pre-Injection 600",
    }


def test_parse_groups_reads_a_root_group():
    buf = _group_record(0x800009D2, 0xFFFFFFFF, "KMSC National motorcycle racing")
    assert ccc.parse_groups(buf) == {"0x800009D2": "KMSC National motorcycle racing"}


def test_parse_groups_skips_a_record_without_the_zero_padding():
    assert ccc.parse_groups(_group_record(pad=b"\x00" * 7 + b"\x01")) == {}


def test_parse_groups_skips_empty_unprintable_and_repeated_names():
    buf = (
        bytes(range(256))
        + _group_record(0x800009D4, name="Sidecars")
        + _group_record(0x800009D5, name="")
        + _group_record(0x800009D6, name="\x07\x01")
        + _group_record(0x800009D4, name="Sidecars again")
        + _group_record(0x800009D7)[:-20]
    )
    assert ccc.parse_groups(buf) == {"0x800009D4": "Sidecars"}


def test_parse_groups_ignores_run_records():
    assert ccc.parse_groups(bytes(16) + _run_record() + bytes(16)) == {}


def test_run_table_record_is_parsed():
    buf = bytes(16) + _run_record() + bytes(16)
    assert ccc.parse_runs(buf) == [
        {"run_id": "0x40002806", "group_id": "0x80000985", "name": "Race 7 - 2nd Race"},
    ]


def test_run_table_ids_are_uppercase_hex_like_push_tags():
    (run,) = ccc.parse_runs(_run_record(0x400027FB, 0x8000098A, "Qualifying 4"))
    assert (run["run_id"], run["group_id"]) == ("0x400027FB", "0x8000098A")


def test_run_table_skips_noise_and_truncated_records():
    good = _run_record(0x40002805, name="Race 6 - AMENDED GRID")
    not_a_run = _run_record(0x10002806)
    no_name = _run_record(0x40002807, name="")
    unprintable = _run_record(0x40002808, name="\x07\x01")
    repeat = _run_record(0x40002805, name="Race 6")
    truncated = _run_record(0x40002809)[:-12]
    buf = (
        bytes(range(256)) + not_a_run + good + no_name + unprintable + repeat
        + truncated
    )
    assert ccc.parse_runs(buf) == [
        {"run_id": "0x40002805", "group_id": "0x80000985", "name": "Race 6 - AMENDED GRID"},
    ]


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
) + _run_record()
REGISTRY_ENTRIES = [
    _triple("11", reg_id="aaaa1111"),
    _triple("22", "Test Sports Trophy", "TT", reg_id="bbbb2222"),
]


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
    assert msg["runs"] == [
        {"run_id": "0x40002806", "group_id": "0x80000985", "name": "Race 7 - 2nd Race"},
    ]
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
    assert calls == [
        REGISTRY_ENTRIES, REGISTRY_ENTRIES, [_triple("33", code="TN", reg_id="cccc3333")],
    ]
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


def test_a_marker_inside_model_bytes_is_not_taken_for_a_push():
    """Model bytes go through the push parser so interleaved pushes are kept;
    the marker alone, without a record's framing and header, yields nothing."""
    body = b"\x00\x13model-internal\x00table\x01"
    model = (
        _registry_record() + b"datamanager" + struct.pack("<I", len(body)) + body
        + b"datamanager\x02" + _registry_record(tx=22)
    )
    assert ccc.PushParser().feed(model) == []


@pytest.mark.asyncio
async def test_a_preload_larger_than_the_server_accepts_is_not_forwarded(monkeypatch, caplog):
    """It would be answered with a 413 on every retry until it aged out."""
    monkeypatch.setattr(ccc, "MAX_PRELOAD_BYTES", len(json.dumps(REGISTRY_ENTRIES)) - 1)
    writer = FakeWriter()
    opened, sleeps = _harness(monkeypatch, [(_pulling(writer), writer)])
    batches = []
    client = ccc.ClassCodeClient("timing-host", _collect(batches), **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: len(writer.writes) >= 5)
        await asyncio.sleep(0.2)
    finally:
        await _finish(task)
    assert batches == []
    assert "over the server's" in caplog.text
    assert len(opened) == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

def _run_state(name="Race 6 - AMENDED GRID", run_id=0x40002805, state="started") -> bytes:
    """One run-state notice as the host frames it on the stream."""
    text = (
        f"Run '{name}' [0x{run_id:08X}] is {state} - Event 'Test Meeting'"
    ).encode()
    return (
        struct.pack("<I", 14) + b"runstatechange" + struct.pack("<I", len(text)) + text
        + b"d" + struct.pack("<I", run_id)
    )


def test_run_state_parser_reads_a_started_run():
    assert ccc.RunStateParser().feed(bytes(8) + _run_state() + bytes(8)) == [
        ccc.RunState("0x40002805", "Race 6 - AMENDED GRID", "started", "Test Meeting"),
    ]


def _notice(text: str) -> bytes:
    """A run-state notice carrying *text* verbatim."""
    raw = text.encode()
    return struct.pack("<I", 14) + b"runstatechange" + struct.pack("<I", len(raw)) + raw


def test_run_state_parser_reads_the_event_name():
    (run,) = ccc.RunStateParser().feed(_notice(
        "Run 'Race 2 - 1st Race' [0x400035E0] is started"
        " - Event 'Scottish Championship Pre-Injection 600'"
    ))
    assert run == ccc.RunState(
        "0x400035E0", "Race 2 - 1st Race", "started",
        "Scottish Championship Pre-Injection 600",
    )


def test_run_state_parser_keeps_apostrophes_in_the_event_name():
    """Captured: the Event ends the notice, so commas and apostrophes in it
    survive whole."""
    (run,) = ccc.RunStateParser().feed(_notice(
        "Run 'Race 5' [0x40002805] is stopped"
        " - Event 'Future Classics, Modern Classics, Open, New Millennium, 7's'"
    ))
    assert run.name == "Race 5"
    assert run.event == "Future Classics, Modern Classics, Open, New Millennium, 7's"


def test_run_state_parser_tolerates_a_notice_without_an_event():
    (run,) = ccc.RunStateParser().feed(_notice("Run 'Race 5' [0x40002805] is started"))
    assert run == ccc.RunState("0x40002805", "Race 5", "started", "")


def test_run_state_parser_reads_a_name_containing_an_apostrophe():
    """The match is anchored on the closing ``' [id] is``, so an apostrophe in
    the run or event name does not end the run name early or late."""
    (run,) = ccc.RunStateParser().feed(_run_state("Driver's Trophy - Collector's Race"))
    assert run.name == "Driver's Trophy - Collector's Race"


def test_run_state_parser_joins_a_frame_split_across_reads():
    data = _run_state(state="stopped")
    parser = ccc.RunStateParser()
    out = parser.feed(data[:7]) + parser.feed(data[7:30]) + parser.feed(data[30:])
    assert out == [
        ccc.RunState("0x40002805", "Race 6 - AMENDED GRID", "stopped", "Test Meeting"),
    ]


def test_run_state_parser_ignores_other_text():
    text = b"Autosave: 4184 runs saved"
    data = struct.pack("<I", 14) + b"runstatechange" + struct.pack("<I", len(text)) + text
    assert ccc.RunStateParser().feed(data) == []


def _recording():
    """Record what *on_batch* is handed, except the announcements clear every stop sends."""
    calls = []

    async def on_batch(msg):
        if msg["type"] != "announcements":
            calls.append(msg)
    return calls, on_batch


@pytest.mark.asyncio
async def test_a_started_run_is_forwarded_before_the_pushes_of_its_burst(monkeypatch):
    gate = asyncio.Event()
    data = _push("added", "0x40002805", _fields()) + _run_state()
    _harness(monkeypatch, [(ChunkReader([IDENT_FRAME, gate, data]), FakeWriter())])
    calls, on_batch = _recording()
    client = ccc.ClassCodeClient("timing-host", on_batch, **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(0.05)
        gate.set()
        await _until(lambda: len(calls) >= 2)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert [c["type"] for c in calls] == ["class_code_run", "class_codes"]
    run = calls[0]
    assert (run["run_id"], run["name"]) == ("0x40002805", "Race 6 - AMENDED GRID")
    assert isinstance(run["age_seconds"], float) and run["age_seconds"] >= 0


@pytest.mark.asyncio
async def test_a_start_notice_forwards_its_event_name(monkeypatch):
    task, host, writer, calls, client = await _running(monkeypatch)
    try:
        host.queue.append(_run_state())
        await _until(lambda: _runs_sent(calls))
    finally:
        await _finish(task)
    (run,) = _runs_sent(calls)
    assert (run["run_id"], run["event"]) == ("0x40002805", "Test Meeting")


@pytest.mark.asyncio
async def test_a_start_notice_without_an_event_sends_no_event_field(monkeypatch):
    task, host, writer, calls, client = await _running(monkeypatch)
    try:
        host.queue.append(_notice("Run 'Race 5' [0x40002805] is started"))
        await _until(lambda: _runs_sent(calls))
    finally:
        await _finish(task)
    (run,) = _runs_sent(calls)
    assert run["name"] == "Race 5"
    assert "event" not in run


@pytest.mark.asyncio
async def test_a_stopped_run_is_not_forwarded(monkeypatch):
    gate = asyncio.Event()
    reader = ChunkReader([IDENT_FRAME, gate, _run_state(state="stopped")])
    _harness(monkeypatch, [(reader, FakeWriter())])
    calls, on_batch = _recording()
    client = ccc.ClassCodeClient("timing-host", on_batch, **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.sleep(0.2)
    finally:
        await _finish(task)
    assert calls == []
    assert client._run is None


@pytest.mark.asyncio
async def test_a_failed_run_delivery_is_retried_and_a_newer_run_replaces_it(monkeypatch):
    gate = asyncio.Event()
    reader = ChunkReader([IDENT_FRAME, gate, _run_state("Race 6")])
    opened, sleeps = _harness(monkeypatch, [(reader, FakeWriter())])
    calls = []

    async def on_batch(msg):
        calls.append(msg["name"])
        if len(calls) <= 2:
            raise ConnectionError("server unreachable")

    client = ccc.ClassCodeClient(
        "timing-host", on_batch,
        **{**FAST, "keepalive_interval": 0.05, "retry_initial": 0.1},
    )
    task = asyncio.ensure_future(client.run())
    try:
        gate.set()
        await _until(lambda: len(calls) >= 2)
        reader._items.append(_run_state("Race 7", run_id=0x40002806))
        await _until(lambda: len(calls) >= 3)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert calls == ["Race 6", "Race 6", "Race 7"]
    assert client._run is None
    assert len(opened) == 1
    assert sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_id, forwarded", [
    (0x40002805, []),
    (0x40002804, ["0x40002805"]),
])
async def test_a_run_stopped_before_its_start_was_delivered_is_not_forwarded(
    monkeypatch, stopped_id, forwarded
):
    """Only the matching stop clears it; another run's stop leaves it alone."""
    gate = asyncio.Event()
    data = _run_state() + _run_state("Race 5", run_id=stopped_id, state="stopped")
    _harness(monkeypatch, [(ChunkReader([IDENT_FRAME, gate, data]), FakeWriter())])
    calls, on_batch = _recording()
    client = ccc.ClassCodeClient("timing-host", on_batch, **FAST)
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.sleep(0.2)
    finally:
        await _finish(task)
    assert [c["run_id"] for c in calls] == forwarded


@pytest.mark.asyncio
@pytest.mark.parametrize("stops", [
    [0x40002805],
    # A later, unrelated stop must not hide the in-flight run's own.
    [0x40002805, 0x40002806],
])
async def test_a_run_stopped_while_its_failed_delivery_was_in_flight_is_not_retried(
    monkeypatch, stops
):
    gate = asyncio.Event()
    reader = ChunkReader([IDENT_FRAME, gate, _run_state()])
    _harness(monkeypatch, [(reader, FakeWriter())])
    calls = []

    async def on_batch(msg):
        if msg["type"] == "announcements":
            return
        calls.append(msg["run_id"])
        if len(calls) == 1:
            reader._items.append(b"".join(
                _run_state("Race", run_id=r, state="stopped") for r in stops
            ))
            await asyncio.sleep(0.15)  # the hold loop reads the stop meanwhile
            raise ConnectionError("server unreachable")

    client = ccc.ClassCodeClient(
        "timing-host", on_batch,
        **{**FAST, "keepalive_interval": 0.05, "retry_initial": 0.05},
    )
    task = asyncio.ensure_future(client.run())
    try:
        gate.set()
        await _until(lambda: calls)
        await asyncio.sleep(0.4)
    finally:
        await _finish(task)
    assert calls == ["0x40002805"]
    assert client._run is None


@pytest.mark.asyncio
async def test_a_run_stopped_while_the_preload_is_delivered_is_not_forwarded(monkeypatch):
    """The preload goes first in a flush; a stop read meanwhile must still
    find the started run pending."""
    writer = FakeWriter()
    reader = _pulling(writer, registry=REGISTRY + _run_state())
    _harness(monkeypatch, [(reader, writer)])
    calls = []

    async def on_batch(msg):
        if msg["type"] == "announcements":
            return
        calls.append(msg["type"])
        if msg["type"] == "class_code_preload":
            reader._then.append(_run_state(state="stopped"))
            await asyncio.sleep(0.15)  # the hold loop reads the stop meanwhile

    client = ccc.ClassCodeClient(
        "timing-host", on_batch, **{**FAST, "keepalive_interval": 0.05}
    )
    task = asyncio.ensure_future(client.run())
    try:
        await _until(lambda: calls)
        await asyncio.sleep(0.4)
    finally:
        await _finish(task)
    assert calls == ["class_code_preload"]
    assert client._run is None


# ---------------------------------------------------------------------------
# Announcements: view records and frames
# ---------------------------------------------------------------------------

SESSION = {"token": TOKEN, "handle": HANDLE, "unit": UNIT}


def _with_identity(rec: bytes) -> bytes:
    """Return a captured view record, its identity zeroed, with the test's written in."""
    out = bytearray(rec)
    out[4:6] = HANDLE
    out[8:14] = UNIT
    out[22:28] = TOKEN
    return bytes(out)


# A subscribe record the timing console sent (view 1, the qualifying results)
# and a close it sent for view 3, identity zeroed.
CONSOLE_VIEW_OPEN = bytes.fromhex(
    "2180050000000000000000000000000006012000000000000000000000000100"
    "0f0500000000000000000000000000000000000000000000000000"
    "89000000" "150000006c67566965775f5175616c696679526573756c7473" "01000000" "04000000"
    "130000004c69766553656374696f6e446563696d616c73" "0100000031"
    "0f00000053656374696f6e446563696d616c73" "0100000033"
    "0d0000005370656564446563696d616c73" "0100000031"
    "08000000556e697175654944" "0a00000034323934393637323935" "00000000"
)
CONSOLE_VIEW_CLOSE = bytes.fromhex(
    "2280050000000000000000000000000006012000000000000000000000000100"
    "0f0500000000000000000000000000000000000000000000000000"
    "10000000" "00000000" "03000000" "00000000" "00000000"
)

# The header of a frame the host sends on an Announcements view, identity
# zeroed; the opcode goes in front.
_ANN_HEADER = bytes.fromhex(
    "060120000000" "000000000000" "0000" "0500" "0000" "0000" "000000000000" "000001000f05"
) + bytes(25)

# Captured frames: the reply to view 42's subscribe holding "Test 4", the
# push of an edit to "Test 5 edited" sent to view 105, and the reply to
# view 43's subscribe after "Test 4" was deleted.
ANN_REPLY_TEST_4 = bytes.fromhex("2480") + _ANN_HEADER + bytes.fromhex(
    "a00000002a00000001000000000000000d000000416e6e6f756e63656d656e74730d000000"
    "416e6e6f756e63656d656e7473010000000100000000000000ffff600000000100000000"
    "01000000310c8ac276ec5c0600000a00000030332f31302f323032360c8ac276ec5c0600"
    "000800000031303a34323a3134100000004f6666696369616c206d657373616765000600"
    "00005465737420340000000000000100000030"
)
ANN_MODIFIED_TEST_5 = bytes.fromhex("2680") + _ANN_HEADER + bytes.fromhex(
    "a70000006900000001000000000000000d000000416e6e6f756e63656d656e74730d000000"
    "416e6e6f756e63656d656e7473010000000200000000000000ffff670000000200000000"
    "0100000032680f0e87ec5c0600000a00000030332f31302f32303236680f0e87ec5c0600"
    "000800000031303a34363a3438100000004f6666696369616c206d657373616765000d00"
    "0000546573742035206564697465640000000000000100000030"
)
ANN_EMPTY_REPLY = bytes.fromhex("2480") + _ANN_HEADER + bytes.fromhex(
    "320000002b00000001000000000000000d000000416e6e6f756e63656d656e74730d000000"
    "416e6e6f756e63656d656e747300000000"
)
TEST_4_ROW = {
    "text": "Test 4", "ticks": 1791020534761996, "date": "03/10/2026",
    "time": "10:42:14", "type": "Official message", "priority": "0",
}


def _ann_row(index, text, ticks, *, date="03/10/2026", time="10:42:14", priority="0"):
    stamp = struct.pack("<Q", ticks) + b"\x00"
    return (
        struct.pack("<I", index) + b"\x00" + _s(str(index))
        + stamp + _s(date) + stamp + _s(time)
        + _s("Official message") + b"\x00" + _s(text) + bytes(6) + _s(priority)
    )


def _ann_frame(opcode, view_id, rows=(), *, count=None):
    """One Announcements frame in the captured layout.

    *rows* are ``(text, ticks)`` pairs, concatenated: the bytes between two
    rows have never been captured.  *count* overrides the row count.
    """
    body = b"".join(_ann_row(i + 1, text, ticks) for i, (text, ticks) in enumerate(rows))
    block = struct.pack("<III", view_id, 1, 0) + _s("Announcements") * 2
    block += struct.pack("<I", len(rows) if count is None else count)
    if rows:
        block += struct.pack("<II", len(rows), 0) + b"\xff\xff"
        block += struct.pack("<I", len(body)) + body
    return opcode + _ANN_HEADER + struct.pack("<I", len(block)) + block


def test_ann_frame_helper_builds_the_captured_layout():
    assert len(b"\x24\x80" + _ANN_HEADER) == 59
    assert _ann_frame(b"\x24\x80", 42, [("Test 4", TEST_4_ROW["ticks"])]) == ANN_REPLY_TEST_4
    assert _ann_frame(b"\x24\x80", 43) == ANN_EMPTY_REPLY


def test_view_open_rebuilds_the_captured_console_record():
    rec = ccc.build_view_open(
        SESSION, view_id=1, name="lgView_QualifyResults",
        params=[("LiveSectionDecimals", "1"), ("SectionDecimals", "3"),
                ("SpeedDecimals", "1"), ("UniqueID", "4294967295")],
    )
    assert len(rec) == 200
    assert rec == _with_identity(CONSOLE_VIEW_OPEN)


def test_view_close_rebuilds_the_captured_console_record():
    rec = ccc.build_view_close(SESSION, view_id=3)
    assert len(rec) == 79
    assert rec == _with_identity(CONSOLE_VIEW_CLOSE)


def test_announcement_parser_reads_a_reply_with_one_row():
    frames = ccc.AnnouncementParser().feed(bytes(100) + ANN_REPLY_TEST_4 + bytes(10))
    assert frames == [ccc.AnnouncementFrame("reply", 42, [TEST_4_ROW])]


def test_announcement_parser_reads_an_empty_reply():
    assert ccc.AnnouncementParser().feed(ANN_EMPTY_REPLY) == [
        ccc.AnnouncementFrame("reply", 43, []),
    ]


def test_announcement_parser_reads_two_rows_in_order():
    data = _ann_frame(b"\x24\x80", 7, [("First", 100), ("Second", 200)])
    (frame,) = ccc.AnnouncementParser().feed(data)
    assert [(r["text"], r["ticks"]) for r in frame.rows] == [("First", 100), ("Second", 200)]


def test_announcement_parser_reads_text_longer_than_a_registry_string():
    text = "Long announcement " * 40
    (frame,) = ccc.AnnouncementParser().feed(_ann_frame(b"\x24\x80", 7, [(text, 1)]))
    assert frame.rows[0]["text"] == text


def test_announcement_parser_classifies_pushes_by_opcode():
    row = [("Test 4", TEST_4_ROW["ticks"])]
    data = (
        _ann_frame(b"\x25\x80", 105, row) + ANN_MODIFIED_TEST_5
        + _ann_frame(b"\x27\x80", 105, row) + _ann_frame(b"\x99\x80", 105, row)
    )
    assert ccc.AnnouncementParser().feed(data) == [
        ccc.AnnouncementFrame("added", 105, None),
        ccc.AnnouncementFrame("modified", 105, None),
        ccc.AnnouncementFrame("deleted", 105, None),
        ccc.AnnouncementFrame("changed", 105, None),
    ]


def test_announcement_parser_withholds_a_reply_whose_row_count_disagrees(caplog):
    data = _ann_frame(b"\x24\x80", 9, [("Test 4", 1)], count=2)
    assert ccc.AnnouncementParser().feed(data) == [ccc.AnnouncementFrame("reply", 9, None)]
    assert "withheld" in caplog.text


def test_announcement_parser_joins_a_frame_split_across_reads():
    parser = ccc.AnnouncementParser()
    data = bytes(30) + ANN_REPLY_TEST_4
    out = []
    for i in range(0, len(data), 7):
        out += parser.feed(data[i:i + 7])
    assert out == [ccc.AnnouncementFrame("reply", 42, [TEST_4_ROW])]


def test_announcement_parser_ignores_the_column_definition_frame():
    """The host's ``21 80`` frame defines the view's columns and carries no
    title pair, so it is never taken for a frame."""
    data = b"\x21\x80" + _ANN_HEADER + struct.pack("<I", 40) + _s("lgHeader_Announcement") + bytes(15)
    assert ccc.AnnouncementParser().feed(data) == []


# ---------------------------------------------------------------------------
# Announcements: the subscription loop
# ---------------------------------------------------------------------------

RUN_ID = "0x40002805"
RUN_DECIMAL = "1073752069"
ANN_FAST = {**FAST, "announce_resubscribe_delay": 0.02, "announce_refresh_interval": 10.0}


class ViewHost:
    """A host that sends its identity frame, then answers every view open.

    Each open written is answered with a ``24 80`` reply on that view
    holding the current *rows*, ``(text, ticks)`` pairs.  Bytes appended to
    *queue* are sent as they come; a queued ``b""`` is EOF.
    """

    def __init__(self, writer, rows=()):
        self._writer = writer
        self.rows = list(rows)
        self.queue = []
        self._identified = False
        self._answered = 0

    async def read(self, n):
        while True:
            if not self._identified and self._writer.writes:
                self._identified = True
                return IDENT_FRAME
            opens = _view_opens(self._writer)
            if self._identified and len(opens) > self._answered:
                view_id = opens[self._answered][0]
                self._answered += 1
                return _ann_frame(b"\x24\x80", view_id, self.rows)
            if self.queue:
                return self.queue.pop(0)
            await asyncio.sleep(0.002)


def _view_opens(writer):
    """Return ``(view id, UniqueID)`` for every view open written."""
    out = []
    for w in writer.writes:
        if w[:2] == b"\x21\x80":
            (n,) = struct.unpack_from("<I", w, 63)
            (view_id,) = struct.unpack_from("<I", w, 67 + n)
            (k,) = struct.unpack_from("<I", w, 75 + n)
            (v,) = struct.unpack_from("<I", w, 79 + n + k)
            out.append((view_id, w[83 + n + k:83 + n + k + v].decode()))
    return out


def _view_closes(writer):
    return [struct.unpack_from("<I", w, 67)[0] for w in writer.writes if w[:2] == b"\x22\x80"]


def _announcements(calls):
    return [[r["text"] for r in c["rows"]] for c in calls if c["type"] == "announcements"]


async def _running(monkeypatch, host_rows=(), *, on_batch=None, connections=None, **knobs):
    """Start a client against a :class:`ViewHost`.

    Returns ``(task, host, writer, calls, client)`` for the first connection.
    """
    if connections is None:
        writer = FakeWriter()
        connections = [(ViewHost(writer, host_rows), writer)]
    host, writer = connections[0]
    _harness(monkeypatch, connections, stop_after=knobs.pop("stop_after", 1))
    calls = []
    if on_batch is None:
        async def on_batch(msg):
            calls.append(msg)
    client = ccc.ClassCodeClient("timing-host", on_batch, **{**ANN_FAST, **knobs})
    task = asyncio.ensure_future(client.run())
    await _until(lambda: host._identified)
    return task, host, writer, calls, client


@pytest.mark.asyncio
async def test_a_started_run_subscribes_its_announcements_with_the_decimal_run_id(monkeypatch):
    task, host, writer, calls, _ = await _running(monkeypatch)
    try:
        host.queue.append(_run_state())
        await _until(lambda: _view_opens(writer))
    finally:
        await _finish(task)
    (rec,) = [w for w in writer.writes if w[:2] == b"\x21\x80"]
    assert rec == ccc.build_view_open(
        SESSION, view_id=1, name="lgView_Announcements", params=[("UniqueID", RUN_DECIMAL)]
    )


@pytest.mark.asyncio
async def test_the_subscription_reply_rows_are_forwarded_as_announcements(monkeypatch):
    task, host, writer, calls, _ = await _running(monkeypatch, [("Track clear", 5)])
    try:
        host.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
    finally:
        await _finish(task)
    (msg,) = [c for c in calls if c["type"] == "announcements"]
    key = msg.pop("start_key")
    assert isinstance(key, str) and len(key) == 32
    # The start forwarded for the run names the same start.
    (run,) = [c for c in calls if c["type"] == "class_code_run"]
    assert run["start_key"] == key
    assert msg == {"type": "announcements", "run_id": RUN_ID, "name": "Race 6 - AMENDED GRID",
                   "event": "Test Meeting", "rows": [{
        "text": "Track clear", "ticks": 5, "date": "03/10/2026", "time": "10:42:14",
        "type": "Official message", "priority": "0",
    }]}
    # The run goes first, so the server holds the run binding.
    assert [c["type"] for c in calls] == ["class_code_run", "announcements"]


@pytest.mark.asyncio
async def test_a_delete_push_triggers_a_resubscribe_whose_reply_is_the_truth(monkeypatch):
    """The delete push still carries the deleted row, so it is never read as
    the table; a fresh subscription's reply is."""
    task, host, writer, calls, _ = await _running(monkeypatch, [("Track clear", 5)])
    try:
        host.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
        host.rows = []
        host.queue.append(_ann_frame(b"\x27\x80", 1, [("Track clear", 5)]))
        await _until(lambda: len(_announcements(calls)) >= 2)
        await _until(lambda: _view_closes(writer))
    finally:
        await _finish(task)
    assert _announcements(calls) == [["Track clear"], []]
    assert [v for v, _ in _view_opens(writer)] == [1, 2]
    assert _view_closes(writer) == [1]


@pytest.mark.asyncio
async def test_the_reply_to_a_resubscribe_does_not_trigger_another(monkeypatch):
    task, host, writer, calls, _ = await _running(monkeypatch, [("Track clear", 5)])
    try:
        host.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
        host.queue.append(_ann_frame(b"\x25\x80", 1, [("Track clear", 5)]))
        await _until(lambda: len(_view_opens(writer)) >= 2)
        await asyncio.sleep(0.3)
    finally:
        await _finish(task)
    assert len(_view_opens(writer)) == 2


@pytest.mark.asyncio
async def test_pushes_to_a_view_no_longer_held_are_ignored(monkeypatch):
    task, host, writer, calls, _ = await _running(monkeypatch, [("Track clear", 5)])
    try:
        host.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
        host.queue.append(_ann_frame(b"\x25\x80", 99, [("Track clear", 5)]))
        await asyncio.sleep(0.2)
    finally:
        await _finish(task)
    assert len(_view_opens(writer)) == 1


@pytest.mark.asyncio
async def test_a_stopped_run_forwards_empty_announcements_and_closes_its_view(monkeypatch):
    task, host, writer, calls, _ = await _running(monkeypatch, [("Track clear", 5)])
    try:
        host.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
        host.queue.append(_run_state(state="stopped"))
        await _until(lambda: len(_announcements(calls)) >= 2)
        await _until(lambda: _view_closes(writer))
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert _announcements(calls) == [["Track clear"], []]
    assert calls[-1]["run_id"] == RUN_ID
    assert _view_closes(writer) == [1]
    assert len(_view_opens(writer)) == 1


@pytest.mark.asyncio
async def test_a_new_run_closes_the_previous_runs_view(monkeypatch):
    task, host, writer, calls, _ = await _running(monkeypatch, [("Track clear", 5)])
    try:
        host.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
        host.queue.append(_run_state("Race 7", run_id=0x40002806))
        await _until(lambda: len(_announcements(calls)) >= 2)
    finally:
        await _finish(task)
    assert _view_opens(writer) == [(1, RUN_DECIMAL), (2, "1073752070")]
    assert _view_closes(writer) == [1]
    assert [c["run_id"] for c in calls if c["type"] == "announcements"] == [
        RUN_ID, "0x40002806",
    ]


@pytest.mark.asyncio
async def test_a_reconnect_resubscribes_the_remembered_run(monkeypatch):
    first, second = FakeWriter(), FakeWriter()
    host1, host2 = ViewHost(first, [("Track clear", 5)]), ViewHost(second, [("Track clear", 5)])
    task, _, _, calls, _ = await _running(
        monkeypatch, connections=[(host1, first), (host2, second)], stop_after=2,
    )
    try:
        host1.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
        host1.queue.append(b"")
        await _until(lambda: _view_opens(second))
        await _until(lambda: len(_announcements(calls)) >= 2)
    finally:
        await _finish(task)
    assert _view_opens(second) == [(1, RUN_DECIMAL)]
    assert _announcements(calls) == [["Track clear"], ["Track clear"]]


@pytest.mark.asyncio
async def test_announcements_are_refreshed_after_the_refresh_interval(monkeypatch):
    task, host, writer, calls, _ = await _running(
        monkeypatch, [("Track clear", 5)], announce_refresh_interval=0.15,
    )
    try:
        host.queue.append(_run_state())
        await _until(lambda: len(_announcements(calls)) >= 3)
    finally:
        await _finish(task)
    assert [v for v, _ in _view_opens(writer)][:3] == [1, 2, 3]
    assert _view_closes(writer)[:2] == [1, 2]


@pytest.mark.asyncio
async def test_announcements_carry_the_runs_event_so_a_server_can_restore_it(monkeypatch):
    task, host, writer, calls, _ = await _running(monkeypatch, [("Track clear", 5)])

    def sent(run_id):
        return [
            c for c in calls
            if c["type"] == "announcements" and c["run_id"] == run_id
            and not c.get("stopped")
        ]
    try:
        host.queue.append(_run_state())
        await _until(lambda: sent("0x40002805"))
        host.queue.append(_notice("Run 'Race 8' [0x40002806] is started"))
        await _until(lambda: sent("0x40002806"))
    finally:
        await _finish(task)
    assert all(a["event"] == "Test Meeting" for a in sent("0x40002805"))
    assert all("event" not in a for a in sent("0x40002806"))


@pytest.mark.asyncio
async def test_an_unanswered_subscription_is_sent_again_on_a_new_view(monkeypatch):
    writer = FakeWriter()

    class SilentHost(ViewHost):
        async def read(self, n):
            if not self._identified and self._writer.writes:
                self._identified = True
                return IDENT_FRAME
            while not self.queue:
                await asyncio.sleep(0.002)
            return self.queue.pop(0)

    host = SilentHost(writer)
    task, _, _, calls, _ = await _running(
        monkeypatch, connections=[(host, writer)], announce_reply_timeout=0.1,
    )
    try:
        host.queue.append(_run_state())
        await _until(lambda: len(_view_opens(writer)) >= 2)
    finally:
        await _finish(task)
    assert [v for v, _ in _view_opens(writer)][:2] == [1, 2]
    assert _view_closes(writer)[:1] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", [False, True])
async def test_a_failed_announcements_delivery_is_retried_and_a_newer_one_replaces_it(
    monkeypatch, newer
):
    attempts = []
    host_ref = []

    async def on_batch(msg):
        if msg["type"] != "announcements":
            return
        attempts.append([r["text"] for r in msg["rows"]])
        if len(attempts) == 1:
            if newer:
                host = host_ref[0]
                host.rows = [("Second", 6)]
                host.queue.append(_ann_frame(b"\x25\x80", 1, [("Second", 6)]))
                await asyncio.sleep(0.1)  # the hold loop resubscribes meanwhile
            raise ConnectionError("server unreachable")

    task, host, writer, _, _ = await _running(
        monkeypatch, [("First", 5)], on_batch=on_batch,
        retry_initial=0.05, keepalive_interval=0.05,
    )
    host_ref.append(host)
    try:
        host.queue.append(_run_state())
        await _until(lambda: len(attempts) >= 2)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert attempts == ([["First"], ["Second"]] if newer else [["First"], ["First"]])


@pytest.mark.asyncio
async def test_a_stop_clears_announcements_this_process_never_forwarded(monkeypatch):
    """After a relay restart the server may still hold the run's rows, so
    the stop's clear does not depend on what this process forwarded."""
    task, host, writer, calls, _ = await _running(monkeypatch)
    try:
        host.queue.append(_run_state(state="stopped"))
        await _until(lambda: _announcements(calls))
    finally:
        await _finish(task)
    assert [c for c in calls if c["type"] == "announcements"] == [
        {"type": "announcements", "run_id": RUN_ID, "rows": [], "stopped": True,
         "start_key": ""},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("unrelated_first", [True, False])
async def test_an_unrelated_stop_does_not_displace_the_subscribed_runs_clear(
    monkeypatch, unrelated_first
):
    task, host, writer, calls, _ = await _running(monkeypatch, [("Track clear", 5)])
    try:
        host.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
        stops = [
            _run_state("Race 5", run_id=0x40002804, state="stopped"),
            _run_state(state="stopped"),
        ]
        host.queue.append(b"".join(stops if unrelated_first else stops[::-1]))
        await _until(lambda: len(_announcements(calls)) >= 3)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    clears = [c["run_id"] for c in calls if c["type"] == "announcements" and not c["rows"]]
    assert sorted(clears) == ["0x40002804", RUN_ID]


@pytest.mark.asyncio
async def test_a_failed_clear_is_retried_beside_another_runs_clear(monkeypatch):
    attempts = []

    async def on_batch(msg):
        if msg["type"] != "announcements" or msg["rows"]:
            return
        attempts.append(msg["run_id"])
        if attempts.count(RUN_ID) == 1 and msg["run_id"] == RUN_ID:
            raise ConnectionError("server unreachable")

    task, host, writer, _, _ = await _running(
        monkeypatch, [("Track clear", 5)], on_batch=on_batch,
        retry_initial=0.05, keepalive_interval=0.05,
    )
    try:
        host.queue.append(_run_state())
        await asyncio.sleep(0.1)
        host.queue.append(
            _run_state("Race 5", run_id=0x40002804, state="stopped") + _run_state(state="stopped")
        )
        await _until(lambda: attempts.count(RUN_ID) >= 2)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert attempts.count(RUN_ID) == 2
    assert attempts.count("0x40002804") == 1


@pytest.mark.asyncio
async def test_a_failed_delivery_of_an_earlier_runs_rows_is_retried_marked_superseded(
    monkeypatch
):
    """The server keeps rows per run, so the old run's still go — marked."""
    delivered = []
    host_ref = []

    async def on_batch(msg):
        if msg["type"] != "announcements":
            return
        if msg["run_id"] == RUN_ID and not delivered:
            host = host_ref[0]
            host.rows = [("New", 6)]
            host.queue.append(_run_state("Race 7", run_id=0x40002806))
            await asyncio.sleep(0.1)  # the hold loop subscribes the new run meanwhile
            raise ConnectionError("server unreachable")
        delivered.append(
            (msg["run_id"], [r["text"] for r in msg["rows"]], bool(msg.get("superseded")))
        )

    task, host, writer, _, _ = await _running(
        monkeypatch, [("Old", 5)], on_batch=on_batch,
        retry_initial=0.05, keepalive_interval=0.05,
    )
    host_ref.append(host)
    try:
        host.queue.append(_run_state())
        await _until(lambda: delivered)
        await asyncio.sleep(0.3)
    finally:
        await _finish(task)
    # The earlier run's rows still reach the server — it can be the one shown
    # until the boundary — but marked, so they never restore or renew it.
    assert ("0x40002806", ["New"], False) in delivered
    assert (RUN_ID, ["Old"], True) in delivered


@pytest.mark.asyncio
async def test_a_reply_awaited_when_a_change_was_pushed_is_withheld(monkeypatch):
    """A delete pushed before the subscription's reply: that reply may still
    hold the deleted row, so only the follow-up subscription's is forwarded."""
    writer = FakeWriter()

    class PushFirstHost(ViewHost):
        async def read(self, n):
            if self._identified and self._answered == 0 and _view_opens(self._writer):
                view_id = _view_opens(self._writer)[0][0]
                self._answered = 1
                stale = _ann_frame(b"\x24\x80", view_id, self.rows)
                self.rows = []
                return _ann_frame(b"\x27\x80", view_id, [("Withdrawn", 5)]) + stale
            return await super().read(n)

    host = PushFirstHost(writer, [("Withdrawn", 5)])
    task, _, _, calls, _ = await _running(monkeypatch, connections=[(host, writer)])
    try:
        host.queue.append(_run_state())
        await _until(lambda: _announcements(calls))
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert _announcements(calls) == [[]]
    assert [v for v, _ in _view_opens(writer)] == [1, 2]


@pytest.mark.asyncio
async def test_an_earlier_runs_empty_reply_is_dropped_but_its_stop_clear_kept(monkeypatch):
    """Only ``stopped`` marks a clear; an empty reply for a run no longer
    subscribed could otherwise restore that run on the server."""
    delivered = []
    host_ref = []

    async def on_batch(msg):
        if msg["type"] != "announcements":
            return
        if msg["run_id"] == RUN_ID and not msg.get("stopped") and not delivered:
            host_ref[0].queue.append(_run_state("Race 7", run_id=0x40002806))
            await asyncio.sleep(0.1)  # the hold loop subscribes the new run meanwhile
            raise ConnectionError("server unreachable")
        delivered.append((msg["run_id"], bool(msg.get("stopped"))))

    task, host, writer, _, _ = await _running(
        monkeypatch, on_batch=on_batch, retry_initial=0.05, keepalive_interval=0.05,
    )
    host_ref.append(host)
    try:
        host.queue.append(_run_state())
        await _until(lambda: delivered)
        host.queue.append(_run_state(state="stopped"))
        await _until(lambda: (RUN_ID, True) in delivered)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert (RUN_ID, False) not in delivered
    assert ("0x40002806", False) in delivered


# ---------------------------------------------------------------------------
# Announcements: a run picked by name when no start was seen
# ---------------------------------------------------------------------------

PICK_NAME = "Race 2 - 1st Race"
# The newest run of that name, with a letter in its id so a lower-case
# notice can be told from the run table's upper-case one.
PICKED_ID = "0x4000280A"
PICKED_DECIMAL = str(0x4000280A)
PICK_REGISTRY = (
    _registry_record("aaaa1111", 11)
    + _run_record(0x40002801, name=PICK_NAME)
    + _run_record(0x4000280A, name=PICK_NAME)
    + _run_record(0x40002803, name="Race 1 - Qualifying")
)


class PullingViewHost(ViewHost):
    """A :class:`ViewHost` that first answers the registry pull with
    *registry*, split across its answers to records 3 and 4 as
    :func:`_pulling` does."""

    def __init__(self, writer, registry=PICK_REGISTRY, rows=()):
        super().__init__(writer, rows)
        half = len(registry) // 2
        self._pull = {4: registry[:half], 5: registry[half:]}

    async def read(self, n):
        if self._identified:
            while self._pull:
                due = min(self._pull)
                if len(self._writer.writes) >= due:
                    return self._pull.pop(due)
                await asyncio.sleep(0.002)
        return await super().read(n)


async def _picking(monkeypatch, *, connections=None, **knobs):
    if connections is None:
        writer = FakeWriter()
        connections = [(PullingViewHost(writer), writer)]
    return await _running(monkeypatch, connections=connections, **knobs)


def _runs_sent(calls):
    return [c for c in calls if c["type"] == "class_code_run"]


def _lowercase_stop(name, run_id, state="stopped") -> bytes:
    """A run-state notice whose id is lower-case hex, which the regex accepts."""
    text = f"Run '{name}' [0x{run_id:08x}] is {state} - Event 'Test Meeting'".encode()
    return (
        struct.pack("<I", 14) + b"runstatechange" + struct.pack("<I", len(text)) + text
        + b"d" + struct.pack("<I", run_id)
    )


@pytest.mark.asyncio
async def test_a_relay_that_saw_no_start_picks_the_newest_run_named_as_the_session(
    monkeypatch,
):
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _view_opens(writer) and _runs_sent(calls))
    finally:
        await _finish(task)
    assert _view_opens(writer) == [(1, PICKED_DECIMAL)]
    (run,) = _runs_sent(calls)
    assert run["run_id"] == PICKED_ID
    assert run["name"] == PICK_NAME
    assert isinstance(run["start_key"], str) and len(run["start_key"]) == 32


@pytest.mark.asyncio
async def test_a_session_known_before_the_pull_is_picked_when_the_run_table_arrives(
    monkeypatch,
):
    # A slow pull, so the session is noted well before the table arrives.
    task, host, writer, calls, client = await _picking(monkeypatch, record_idle=0.2)
    try:
        assert client._runs is None
        client.note_session("5", PICK_NAME)
        assert _view_opens(writer) == []
        await _until(lambda: _view_opens(writer) and _runs_sent(calls))
    finally:
        await _finish(task)
    assert _view_opens(writer) == [(1, PICKED_DECIMAL)]
    assert [r["run_id"] for r in _runs_sent(calls)] == [PICKED_ID]


@pytest.mark.asyncio
async def test_a_real_start_ends_picking_for_good(monkeypatch):
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _runs_sent(calls))
        host.queue.append(_run_state("Race 3", 0x40002807, "started"))
        await _until(lambda: len(_runs_sent(calls)) >= 2)
        await _until(lambda: (2, str(0x40002807)) in _view_opens(writer))
        client.note_session("6", "Race 1 - Qualifying")
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert [r["run_id"] for r in _runs_sent(calls)] == [PICKED_ID, "0x40002807"]
    assert [u for _, u in _view_opens(writer)] == [PICKED_DECIMAL, str(0x40002807)]
    assert client._ann_run == "0x40002807"


@pytest.mark.asyncio
async def test_a_stopped_pick_is_not_picked_again_for_the_same_description(monkeypatch):
    first, second = FakeWriter(), FakeWriter()
    host1, host2 = PullingViewHost(first), PullingViewHost(second)
    task, _, _, calls, client = await _picking(
        monkeypatch, connections=[(host1, first), (host2, second)], stop_after=2,
    )
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _announcements(calls) and _runs_sent(calls))
        host1.queue.append(_lowercase_stop(PICK_NAME, 0x4000280A))
        await _until(lambda: _view_closes(first))
        host1.queue.append(b"")
        # The reconnect pulls the same run table again.
        await _until(lambda: not host2._pull)
        client.note_session("5", PICK_NAME)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    (run,) = _runs_sent(calls)
    (stop,) = [c for c in calls if c["type"] == "announcements" and c.get("stopped")]
    assert stop["rows"] == []
    # Matched despite the case, so the clear names the picked start.
    assert stop["start_key"] == run["start_key"]
    assert _view_closes(first) == [1]
    assert _view_opens(second) == []
    assert client._ann_run is None


@pytest.mark.asyncio
async def test_a_new_description_with_no_matching_run_drops_the_pick(monkeypatch):
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _announcements(calls))
        client.note_session("6", "Not In Table")
        await _until(lambda: _view_closes(writer))
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert _view_closes(writer) == [1]
    assert len(_view_opens(writer)) == 1
    assert client._ann_run is None
    # Its rows are cleared as a stop clears them, under the pick's start.
    (run,) = _runs_sent(calls)
    (clear,) = [c for c in calls if c["type"] == "announcements" and c.get("stopped")]
    assert clear == {
        "type": "announcements", "run_id": PICKED_ID, "rows": [], "stopped": True,
        "start_key": run["start_key"],
    }


@pytest.mark.asyncio
async def test_a_closing_95_and_a_repeated_description_do_not_re_pick(monkeypatch):
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _announcements(calls) and _runs_sent(calls))
        client.note_session("95", PICK_NAME)
        client.note_session("5", PICK_NAME)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert len(_runs_sent(calls)) == 1
    assert len(_view_opens(writer)) == 1


@pytest.mark.asyncio
async def test_a_picked_run_is_resubscribed_after_a_reconnect_without_a_new_start(
    monkeypatch,
):
    first, second = FakeWriter(), FakeWriter()
    host1 = PullingViewHost(first, rows=[("Track clear", 5)])
    host2 = PullingViewHost(second, rows=[("Track clear", 5)])
    task, _, _, calls, client = await _picking(
        monkeypatch, connections=[(host1, first), (host2, second)], stop_after=2,
    )
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _announcements(calls) and _runs_sent(calls))
        host1.queue.append(b"")
        await _until(lambda: _view_opens(second))
        await _until(lambda: len(_announcements(calls)) >= 2)
    finally:
        await _finish(task)
    assert _view_opens(second) == [(1, PICKED_DECIMAL)]
    (run,) = _runs_sent(calls)
    keys = {c["start_key"] for c in calls if c["type"] == "announcements"}
    assert keys == {run["start_key"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["lowercase stop", "session change"])
async def test_a_pick_ended_while_its_failed_delivery_was_in_flight_is_not_retried(
    monkeypatch, ending
):
    attempts = []
    ref = []

    async def on_batch(msg):
        if msg["type"] != "class_code_run":
            return
        attempts.append(msg["run_id"])
        if len(attempts) == 1:
            host, client = ref
            if ending == "lowercase stop":
                host.queue.append(_lowercase_stop(PICK_NAME, 0x4000280A))
            else:
                client.note_session("6", "Not In Table")
            await asyncio.sleep(0.15)  # the hold loop reads the stop meanwhile
            raise ConnectionError("server unreachable")

    task, host, writer, calls, client = await _picking(
        monkeypatch, on_batch=on_batch, retry_initial=0.05,
    )
    ref[:] = [host, client]
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: attempts)
        await asyncio.sleep(0.4)
    finally:
        await _finish(task)
    assert attempts == [PICKED_ID]
    assert client._run is None


@pytest.mark.asyncio
@pytest.mark.parametrize("lowercase", [False, True])
async def test_the_picked_run_starting_keeps_the_picks_start(monkeypatch, lowercase):
    """A session's $B can pick its run before the start notice arrives; that
    start must not mint a second key the server never holds."""
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _announcements(calls) and _runs_sent(calls))
        host.queue.append(
            _lowercase_stop(PICK_NAME, 0x4000280A, "started") if lowercase
            else _run_state(PICK_NAME, 0x4000280A, "started")
        )
        await _until(lambda: client._seen_start)
        # Picking has ended with the real start.
        client.note_session("6", "Race 1 - Qualifying")
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    # The start notice may resend the pick to supply its event, but never
    # under a new key.
    runs = _runs_sent(calls)
    assert {r["start_key"] for r in runs} == {client._ann_start_key}
    assert {r["run_id"] for r in runs} == {PICKED_ID}
    assert client._ann_run == PICKED_ID
    assert len(_view_opens(writer)) == 1


def _picking_with_groups(registry):
    writer = FakeWriter()
    return [(PullingViewHost(writer, registry), writer)]


@pytest.mark.asyncio
async def test_a_picked_run_carries_its_group_name_as_the_event(monkeypatch):
    registry = PICK_REGISTRY + _group_record(0x80000985, name="Test Championship")
    task, host, writer, calls, client = await _picking(
        monkeypatch, connections=_picking_with_groups(registry),
    )
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _runs_sent(calls))
    finally:
        await _finish(task)
    (run,) = _runs_sent(calls)
    assert (run["run_id"], run["event"]) == (PICKED_ID, "Test Championship")


@pytest.mark.asyncio
async def test_a_picked_run_whose_group_is_unknown_sends_no_event(monkeypatch):
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _runs_sent(calls))
    finally:
        await _finish(task)
    (run,) = _runs_sent(calls)
    assert run["run_id"] == PICKED_ID
    assert "event" not in run


@pytest.mark.asyncio
async def test_a_picked_runs_start_notice_supplies_a_missing_event(monkeypatch):
    """A pick that found no group name is resent with the notice's event,
    under the pick's own start key."""
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _runs_sent(calls))
        host.queue.append(_run_state(PICK_NAME, 0x4000280A, "started"))
        await _until(lambda: len(_runs_sent(calls)) == 2)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    first, second = _runs_sent(calls)
    assert "event" not in first
    assert second["event"] == "Test Meeting"
    assert (second["run_id"], second["name"], second["start_key"]) == (
        first["run_id"], first["name"], first["start_key"],
    )
    assert len(_view_opens(writer)) == 1


@pytest.mark.asyncio
async def test_a_picked_runs_start_notice_with_the_same_event_sends_nothing_more(
    monkeypatch,
):
    registry = PICK_REGISTRY + _group_record(0x80000985, name="Test Meeting")
    task, host, writer, calls, client = await _picking(
        monkeypatch, connections=_picking_with_groups(registry),
    )
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _runs_sent(calls))
        host.queue.append(_run_state(PICK_NAME, 0x4000280A, "started"))
        await _until(lambda: client._seen_start)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    (run,) = _runs_sent(calls)
    assert run["event"] == "Test Meeting"


@pytest.mark.asyncio
async def test_a_lowercase_stop_replaces_the_picks_failed_rows(monkeypatch):
    """The clear and the picked run's rows are queued as one run, so rows
    whose delivery failed during the stop are never sent after its clear."""
    sent = []
    ref = []

    async def on_batch(msg):
        if msg["type"] != "announcements":
            return
        sent.append(msg)
        if msg["rows"] and len(sent) == 1:
            ref[0].queue.append(_lowercase_stop(PICK_NAME, 0x4000280A))
            await asyncio.sleep(0.15)  # the hold loop reads the stop meanwhile
            raise ConnectionError("server unreachable")

    writer = FakeWriter()
    host = PullingViewHost(writer, rows=[("Track clear", 5)])
    ref.append(host)
    task, _, _, _, client = await _picking(
        monkeypatch, connections=[(host, writer)], on_batch=on_batch, retry_initial=0.05,
    )
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: any(m.get("stopped") for m in sent))
        await asyncio.sleep(0.3)
    finally:
        await _finish(task)
    assert [bool(m["rows"]) for m in sent] == [True, False]
    assert sent[-1]["run_id"] == PICKED_ID


@pytest.mark.asyncio
async def test_a_stopped_pick_is_not_picked_again_after_another_description(monkeypatch):
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _runs_sent(calls))
        host.queue.append(_lowercase_stop(PICK_NAME, 0x4000280A))
        await _until(lambda: _view_closes(writer))
        client.note_session("6", "Race 1 - Qualifying")
        await _until(lambda: len(_runs_sent(calls)) >= 2)
        client.note_session("7", PICK_NAME)
        await _until(lambda: len(_view_closes(writer)) >= 2)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert [r["run_id"] for r in _runs_sent(calls)] == [PICKED_ID, "0x40002803"]
    assert [u for _, u in _view_opens(writer)] == [PICKED_DECIMAL, str(0x40002803)]
    assert client._ann_run is None


@pytest.mark.asyncio
async def test_a_newer_run_of_a_stopped_picks_name_is_picked_from_a_later_pull(monkeypatch):
    first, second = FakeWriter(), FakeWriter()
    host1 = PullingViewHost(first)
    host2 = PullingViewHost(second, PICK_REGISTRY + _run_record(0x4000280B, name=PICK_NAME))
    task, _, _, calls, client = await _picking(
        monkeypatch, connections=[(host1, first), (host2, second)], stop_after=2,
    )
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _runs_sent(calls))
        host1.queue.append(_run_state(PICK_NAME, 0x4000280A, "stopped"))
        await _until(lambda: _view_closes(first))
        host1.queue.append(b"")
        await _until(lambda: _view_opens(second) and len(_runs_sent(calls)) >= 2)
    finally:
        await _finish(task)
    assert [r["run_id"] for r in _runs_sent(calls)] == [PICKED_ID, "0x4000280B"]
    assert _view_opens(second) == [(1, str(0x4000280B))]


@pytest.mark.asyncio
async def test_a_session_closed_before_the_run_table_arrives_is_not_picked(monkeypatch):
    task, host, writer, calls, client = await _picking(monkeypatch, record_idle=0.2)
    try:
        assert client._runs is None
        client.note_session("5", PICK_NAME)
        client.note_session("95", PICK_NAME)
        await _until(lambda: client._runs is not None)
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    assert _runs_sent(calls) == []
    assert _view_opens(writer) == []


@pytest.mark.asyncio
async def test_a_new_session_number_under_the_same_description_ends_the_pick(monkeypatch):
    """The picked run belongs to the session that picked it; the run table,
    pulled once per connect, cannot hold the new session's run."""
    task, host, writer, calls, client = await _picking(monkeypatch)
    try:
        await _until(lambda: client._runs is not None)
        client.note_session("5", PICK_NAME)
        await _until(lambda: _announcements(calls) and _runs_sent(calls))
        client.note_session("6", PICK_NAME)
        await _until(lambda: _view_closes(writer))
        await asyncio.sleep(0.1)
    finally:
        await _finish(task)
    (run,) = _runs_sent(calls)
    assert len(_view_opens(writer)) == 1
    (clear,) = [c for c in calls if c["type"] == "announcements" and c.get("stopped")]
    assert clear["run_id"] == PICKED_ID and clear["start_key"] == run["start_key"]
    assert client._ann_run is None
