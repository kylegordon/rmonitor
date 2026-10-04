"""Async client for the class codes on the timing host's secondary port.

The rMonitor feed on port 50000 carries a class number and a free-text class
description, but no class code.  The same timing host serves a second port,
``:51738``, that pushes competitor records carrying the code as a discrete
field.  Its record layout was learned by packet capture on an open network;
this module is a clean-room implementation of the client side.

- **Pushes follow entry-list edits** — an entrant added or changed on the
  timing host — and are not repeated.  The connection is otherwise silent,
  and that silence is normal, not a failure.
- **The handshake is three records**, sent in lockstep: each one is written
  and the socket read until it idles before the next goes out.  The session
  handle and unit id in the second and third are read from the server's
  identity frame on every connect, never replayed.
- **Every connect then pulls the host's competitor registry** with two more
  records, the same way.  It fills in entrants whose entry was edited while
  no relay was connected, which no push will ever repeat.
- **The running run's entry list is requested on the held connection**, as
  the timing console requests its results screen, so an entrant whose code
  was never pushed — never edited while a relay was connected — still gets
  it.  The request never opens a connection of its own.
- **Every connect raises a visible notice on the timing operator's screen.**
  So there is no read-silence timeout, a connection is only replaced after it
  fails, and the reconnect delay is long and grows.

Pushed records are deduplicated per run id and entrant and forwarded in
bursts as ``class_codes`` messages carrying the raw join fields; the registry
is forwarded whole as one ``class_code_preload`` message.  The server does
the join.  Nothing here ever derives a code from a class name.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import struct
import time
import uuid
from typing import NamedTuple

log = logging.getLogger(__name__)

PORT = 51738

# The handshake and keepalive records, as captured from a working session on
# an open network.  Every identity and session slot is zeroed here and filled
# at runtime by offset — never by ``bytes.replace``, because a run of zero
# placeholders is ambiguous.  Which slots are identity is established by the
# offsets below.  The 4-byte client-IP slot at offset 71 of records 1B and 2B
# stays zero: the server takes the client address from the socket.
RECORD_0 = bytes.fromhex(
    "0000010416030000000000000000000000200000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000"
)
RECORD_1B = bytes.fromhex(
    "1110000000000000000000000000000000002000000000000000000000000100"
    "0f0500000000000000000000000000000000000000000000000000ba00000000"
    "00000000000000000000000100000020000000160500000038000b0000004d61"
    "696e204d6f64756c650100000000000000060115000000526573756c74204d6f"
    "6e69746f72204d6f64756c650100000000000000080112000000446961676e6f"
    "7374696373204d6f64756c650100000000000000050215000000547261636b53"
    "696d756c61746f72204d6f64756c650100000000000000080210000000416e6e"
    "6f756e636572204d6f64756c65010000000000000000000000"
)
RECORD_2B = bytes.fromhex(
    "1510000000000000000000000000000000002000000000000000000000000100"
    "0f05000000000000000000000000000000000000000000000000003300000000"
    "000000000000000000000020000000060115000000526573756c74204d6f6e69"
    "746f72204d6f64756c6501000000010000000000050000000000000000000000"
    "0000060120000000000000000000000001000f05000000000000000000000000"
    "00000000000000000000000000080000000100000000000101"
)
# The send-model records, sent after 2B: the host answers them with its
# model, which holds the competitor registry.  Record 4 is two records sent
# in one write.  Neither has a client-IP slot.
RECORD_3 = bytes.fromhex(
    "0300050000000000000000000000000006012000000000000000000000000100"
    "0f050000000000000000000000000000000000000000000000000000000000"
)
RECORD_4 = bytes.fromhex(
    "2080050000000000000000000000000006012000000000000000000000000100"
    "0f05000000000000000000000000000000000000000000000000000000000033"
    "000500000000000000000000000000060120000000000000000000000001000f"
    "0500000000000000000000000000000000000000000000000000040000000000"
    "0080"
)
KEEPALIVE = bytes.fromhex(
    "0010000000000000000000000000000000002000000000000000000000000100"
    "0f050000000000000000000000000000000000000000000000000000000000"
)

# Record 0: the 6-byte token, then the machine name in a 64-byte NUL-padded
# slot that carries no length prefix.
RECORD_0_TOKEN = 9
RECORD_0_MACHINE = 21
MACHINE_SLOT_LEN = 64

# A session anchor is ``<handle:2> 00 00 <unit:6>``.
RECORD_1B_ANCHORS = (4,)
RECORD_1B_TOKENS = (22, 63)
RECORD_2B_ANCHORS = (4, 118)
RECORD_2B_TOKENS = (22, 63, 136)
RECORD_3_ANCHORS = (4,)
RECORD_3_TOKENS = (22,)
RECORD_4_ANCHORS = (4, 67)
RECORD_4_TOKENS = (22, 85)
KEEPALIVE_ANCHORS = (4,)
KEEPALIVE_TOKENS = (22,)
IP_SLOT = 71

# The header of the view records (see build_view_open), as captured with its
# identity zeroed; the opcode in its first two bytes is swapped per record.
VIEW_HEADER = bytes.fromhex(
    "2180050000000000000000000000000006012000000000000000000000000100"
    "0f0500000000000000000000000000000000000000000000000000"
)
VIEW_ANCHORS = (4,)
VIEW_TOKENS = (22,)
_OP_VIEW_OPEN = b"\x21\x80"
_OP_VIEW_CLOSE = b"\x22\x80"
ANNOUNCEMENTS_VIEW = "lgView_Announcements"
# The view the timing console opens from its results screen, with these
# parameters before the run's UniqueID.  It answers with one row per entrant
# for any run type, a qualifying run included; see EntryListParser.
RESULTS_VIEW = "lgView_RaceResults"
RESULTS_VIEW_PARAMS = (("LiveSectionDecimals", "1"), ("SectionDecimals", "3"), ("SpeedDecimals", "1"))

_IDENTITY_FRAME_PREFIX = b"\x00\x00\x01\x04"

# A kept entry older than this is dropped rather than retried: a batch the
# server keeps rejecting must not be retried for ever.  The server keeps a
# delivered class-code record for 36 hours, but its preload and runs for 12,
# and an entry held undelivered this long belongs to a session long gone.
MAX_ENTRY_AGE = 12 * 3600

# A model is ~4.5 MB and grows with the host's archive; a stream still going
# past this is not one, and is not buffered further.
MODEL_BUFFER_CAP = 32 * 1024 * 1024

# The server's ingest limit (``client_max_size`` in ``server/server.py``; a
# test holds the two equal).  A preload serialising larger would be answered
# with a 413 on every retry, so it is not forwarded at all.
MAX_PRELOAD_BYTES = 4 * 1024 * 1024

_PUSH_MARKER = b"datamanager"
# A length beyond this is not a real record: the marker literal turned up
# inside binary bytes, so the scan skips past it.
_MAX_RECORD_LEN = 1_000_000
_PUSH_HEADER = re.compile(
    r"^Competitor (added|automatically added|modified) \[([^\]]*)\]:"
)

# A registry record opens with its id as a length-prefixed string, then the
# constants 0 and 1; the length byte must equal the id's length, which the
# parser checks.  Ids are usually 1-8 lowercase hex characters, and some are
# ``Competitio<n>``, so any alphanumeric id of up to 32 characters is accepted.
_REGISTRY_ANCHOR = re.compile(
    rb"([\x01-\x20])\x00\x00\x00([0-9A-Za-z]{1,32})\x00\x00\x00\x00\x01\x00\x00\x00"
)
_MAX_REGISTRY_STR = 255

# A group record in the model: the group id (0x8000xxxx), u8, u32, then the
# parent group's id, or 0xFFFFFFFF for a root; see parse_groups.
_GROUP_ANCHOR = re.compile(
    rb"(..\x00\x80).(....)(..\x00\x80|\xff\xff\xff\xff)", re.DOTALL
)

# A run-table record in the model: 16 bytes, the run id (0x4000xxxx), u32
# flags, the group id (0x8000xxxx), then the run name as a ``str``.  The 16
# bytes in front vary (u32 1 plus 12 bytes, all zero, or other), so they are
# not part of the anchor; see parse_runs.  A lookahead, so matches overlap
# and a false match cannot consume a real record's bytes.
_RUN_ANCHOR = re.compile(rb"(?=(..\x00\x40)(....)(..\x00\x80))", re.DOTALL)
_RUN_LEAD_IN = b"\x01\x00\x00\x00"

# An Announcements view frame is found by the view's title, written twice;
# see AnnouncementParser for the offsets around it.
_ANN_MARKER = b"\x0d\x00\x00\x00Announcements\x0d\x00\x00\x00Announcements"
_ANN_OPCODE_AT = -75
_ANN_LENGTH_AT = -16
_ANN_VIEW_AT = -12
_ANN_COUNT_AT = len(_ANN_MARKER)
_ANN_KINDS = {
    b"\x24\x80": "reply",
    b"\x25\x80": "added",
    b"\x26\x80": "modified",
    b"\x27\x80": "deleted",
}
# A row is found by its date and time columns, each a str after an 8-byte
# timestamp and one byte.
# The operator types the text freely, so it is allowed far longer than a
# registry string.
_MAX_ANN_TEXT = 65535
_ANN_ROW = re.compile(
    rb"(.{8})\x00\x0a\x00\x00\x00(\d\d/\d\d/\d{4}).{8}\x00\x08\x00\x00\x00(\d\d:\d\d:\d\d)",
    re.DOTALL,
)

# A results view frame is found by its view id and the two constants after
# it; see EntryListParser for the offsets around them.
_ENT_OPCODE_AT = -63
_ENT_LENGTH_AT = -4
_ENT_REPLY = b"\x24\x80"
# The view id, 1, 0, two empty titles and the row count.
_ENT_MIN_BLOCK = 24
# CarAdditional0-9 in a row's tail; the code is CarAdditional2.
_ENT_EXTRA_FIELDS = 10
_ENT_CODE_FIELD = 2

# The run-state notice the host announces on the live stream; see RunStateParser.
_RUN_STATE_MARKER = b"\x0e\x00\x00\x00runstatechange"
# The Event always ends the text, so its greedy match runs to the end and keeps
# apostrophes such as ``7's``; a notice without one still matches.
_RUN_STATE_TEXT = re.compile(
    r"^Run '(.*)' \[(0x[0-9A-Fa-f]+)\] is (started|stopped)(?: - Event '(.*)'$)?"
)


class HandshakeError(Exception):
    """The server did not answer the handshake as a compatible host does."""


@functools.cache
def relay_identity() -> tuple[bytes, bytes]:
    """Return this process's ``(token, machine name)`` for the handshake.

    Both come from :func:`uuid.getnode`: the token is its 48 bits as six
    big-endian bytes, and the machine name — what the timing operator sees
    in the connect notice — is ``RELAY-`` plus its last four hex digits.

    Accepted consequences: on a host with several interfaces it may name one
    that is not carrying this connection; when no MAC address is found,
    ``getnode()`` returns a random value with the multicast bit set, which
    changes on every process start.  It is read once per process, so every
    reconnect — and the GUI's in-process restarts — present the same
    identity.  Two relay processes on one machine therefore collide, and the
    second gets no handshake.
    """
    node = uuid.getnode()
    token = node.to_bytes(6, "big")
    machine = f"RELAY-{node & 0xFFFF:04X}".encode("ascii")
    return token, machine


def build_record_0(*, token: bytes, machine: bytes) -> bytes:
    """Return handshake record 0 carrying *token* and *machine*."""
    if len(token) != 6:
        raise ValueError(f"token must be 6 bytes, got {len(token)}")
    if len(machine) > MACHINE_SLOT_LEN:
        raise ValueError(
            f"machine name must be at most {MACHINE_SLOT_LEN} bytes, got {len(machine)}"
        )
    rec = bytearray(RECORD_0)
    rec[RECORD_0_TOKEN:RECORD_0_TOKEN + 6] = token
    rec[RECORD_0_MACHINE:RECORD_0_MACHINE + MACHINE_SLOT_LEN] = machine.ljust(
        MACHINE_SLOT_LEN, b"\0"
    )
    return bytes(rec)


def build_session_record(
    template: bytes,
    *,
    token: bytes,
    handle: bytes,
    unit: bytes,
    anchors: tuple[int, ...],
    tokens: tuple[int, ...],
) -> bytes:
    """Return *template* with the live session fields written in.

    *handle* is written at each anchor offset and *unit* four bytes after it;
    *token* is written at each token offset.  Used for records 1B, 2B, 3 and
    4 and for the keepalive.
    """
    if len(handle) != 2:
        raise ValueError(f"handle must be 2 bytes, got {len(handle)}")
    if len(unit) != 6:
        raise ValueError(f"unit must be 6 bytes, got {len(unit)}")
    if len(token) != 6:
        raise ValueError(f"token must be 6 bytes, got {len(token)}")
    rec = bytearray(template)
    for a in anchors:
        rec[a:a + 2] = handle
        rec[a + 4:a + 10] = unit
    for t in tokens:
        rec[t:t + 6] = token
    return bytes(rec)


def _str(text: str) -> bytes:
    data = text.encode("utf-8")
    return struct.pack("<I", len(data)) + data


def _view_record(session: dict, opcode: bytes, block: bytes) -> bytes:
    rec = bytearray(build_session_record(
        VIEW_HEADER, **session, anchors=VIEW_ANCHORS, tokens=VIEW_TOKENS
    ))
    rec[0:2] = opcode
    return bytes(rec) + struct.pack("<I", len(block)) + block


def build_view_open(
    session: dict, *, view_id: int, name: str, params: list[tuple[str, str]]
) -> bytes:
    """Return the record that subscribes view *view_id* to the host's *name* view.

    *session* is the live ``token``/``handle``/``unit``.  The record is the
    59-byte :data:`VIEW_HEADER` with opcode ``21 80``, then a u32 block
    length and the block — integers u32 little-endian, a ``str`` a u32 length
    then that many bytes::

        str   view name       e.g. ``lgView_Announcements``
        u32   view id         chosen by the client; the host's frames echo it
        u32   param count
        (str key, str value) x param count
        u32   0

    The host answers with the view's rows as they are now, then pushes each
    later change to every view still open.  The layout matches a record the
    timing console itself sends, byte for byte.
    """
    block = _str(name) + struct.pack("<II", view_id, len(params))
    for key, value in params:
        block += _str(key) + _str(value)
    return _view_record(session, _OP_VIEW_OPEN, block + bytes(4))


def build_view_close(session: dict, *, view_id: int) -> bytes:
    """Return the record that closes view *view_id*.

    It is :data:`VIEW_HEADER` with opcode ``22 80``, then the u32 block length
    16 and the block ``u32 0, u32 view id, u32 0, u32 0`` — a record the timing
    console sends between opens, byte for byte.  Two things are inferred, not
    verified: that the second u32 is the view id (the console's closes name
    the ids it opened earlier), and that the host stops pushing to a closed
    view.  Pushes to a view the client no longer holds are ignored anyway.
    """
    return _view_record(session, _OP_VIEW_CLOSE, struct.pack("<IIII", 0, view_id, 0, 0))


def parse_identity_frame(buf: bytes) -> tuple[bytes, bytes] | None:
    """Return ``(unit, handle)`` from the server's identity frame, or *None*.

    The unit id is bytes 9-14 and the session handle bytes 17-18.  The handle
    is per server and changes over time — one host has served ``00 2a``,
    ``58 36`` and ``34 08`` across three days.  A stale handle gets a 2-byte
    answer and no pushes, which looks like a broken protocol rather than a
    stale value, so it is read here on every connect.
    """
    if len(buf) >= 19 and buf[:4] == _IDENTITY_FRAME_PREFIX:
        return bytes(buf[9:15]), bytes(buf[17:19])
    return None


class PushRecord(NamedTuple):
    """One pushed competitor record."""

    kind: str
    run_id: str
    fields: list[str]


class PushParser:
    """Incrementally parse pushed records out of the ``:51738`` byte stream.

    Each record is the literal ``datamanager``, a u32 little-endian length,
    then exactly that many bytes of tab-delimited text, so a record is
    complete when its length is present — however many reads it spans.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[PushRecord]:
        """Absorb *data* and return every competitor record now complete."""
        self._buf += data
        out: list[PushRecord] = []
        while True:
            i = self._buf.find(_PUSH_MARKER)
            if i < 0:
                # Keep a tail that may hold the start of a straddling marker.
                del self._buf[:-(len(_PUSH_MARKER) - 1)]
                break
            start = i + len(_PUSH_MARKER)
            if len(self._buf) < start + 4:
                del self._buf[:i]
                break
            (n,) = struct.unpack_from("<I", self._buf, start)
            if n > _MAX_RECORD_LEN:
                del self._buf[:i + 1]
                continue
            end = start + 4 + n
            if len(self._buf) < end:
                del self._buf[:i]
                break
            body = bytes(self._buf[start + 4:end])
            del self._buf[:end]
            rec = _parse_body(body)
            if rec is not None:
                out.append(rec)
        return out


class RunState(NamedTuple):
    """One run-state change the host announced."""

    run_id: str
    name: str
    state: str
    event: str = ""


class RunStateParser:
    """Incrementally parse run-state notices out of the ``:51738`` byte stream.

    Each notice is the length-prefixed literal ``runstatechange``, a u32
    little-endian length, then that many bytes of text such as ``Run 'Race 6
    - AMENDED GRID' [0x40002805] is started - Event '…'``; the id is uppercase
    hex, as in the pushes' run tags.  The Event is the name of the
    group the run belongs to — the race name the page shows — and is ``""``
    when a notice carries none.  Notices travel outside the
    ``datamanager`` framing, so :class:`PushParser` never sees them, and the
    model a pull returns holds none.  The started run's name equals the
    rMonitor feed's ``$B`` description, whereas the model's run table can
    still hold an older name for the same run.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[RunState]:
        """Absorb *data* and return every run-state notice now complete."""
        self._buf += data
        out: list[RunState] = []
        while True:
            i = self._buf.find(_RUN_STATE_MARKER)
            if i < 0:
                # Keep a tail that may hold the start of a straddling marker.
                del self._buf[:-(len(_RUN_STATE_MARKER) - 1)]
                break
            start = i + len(_RUN_STATE_MARKER)
            if len(self._buf) < start + 4:
                del self._buf[:i]
                break
            (n,) = struct.unpack_from("<I", self._buf, start)
            if n > _MAX_RECORD_LEN:
                del self._buf[:i + 1]
                continue
            end = start + 4 + n
            if len(self._buf) < end:
                del self._buf[:i]
                break
            text = bytes(self._buf[start + 4:end]).decode("utf-8", errors="replace")
            del self._buf[:end]
            m = _RUN_STATE_TEXT.match(text)
            if m is None:
                log.debug("Skipping an unrecognised run-state notice: %.60r", text)
                continue
            out.append(RunState(m.group(2), m.group(1), m.group(3), m.group(4) or ""))
        return out


class AnnouncementFrame(NamedTuple):
    """One frame of an Announcements view.

    *kind* is ``"reply"`` (the answer to a subscribe: the rows as they are
    now), ``"added"``, ``"modified"`` or ``"deleted"`` (pushed on a change)
    or ``"changed"`` (any other opcode).  *rows* is read only for a reply,
    and is *None* for a push or a reply that could not be read whole.
    """

    kind: str
    view_id: int
    rows: list[dict] | None


class AnnouncementParser:
    """Incrementally parse Announcements view frames out of the ``:51738`` stream.

    Every frame is a 59-byte header, its opcode in the first two bytes, then
    a block; integers are u32 little-endian and a ``str`` is a u32 length then
    that many bytes::

        u32   block length    counted from the view id; 0x32 with no rows
        u32   view id         the id the client chose when it subscribed
        u32   1
        u32   0
        str   "Announcements"
        str   "Announcements"
        u32   row count
        u32                   not read
        u32   0
        2 bytes               ff ff
        u32                   bytes to the end of the block
        rows

    and each row::

        u32   index
        1 byte
        str   UniqueID        "0", "1", "2", …
        8 bytes               timestamp: a u64 growing with creation time
        1 byte
        str   date            dd/mm/yyyy
        8 bytes               the same timestamp
        1 byte
        str   time            HH:MM:SS, local
        str   type            "Official message"
        1 byte
        str   text
        6 bytes
        str   priority        "0" in every capture; forwarded raw, never read

    The opcode says what the frame is: ``24 80`` answers a subscribe and is
    the truth, ``25 80``/``26 80``/``27 80`` are pushed to every open view on
    a create, modify and delete — a delete push still carries the deleted
    row, so a push is only ever a sign that something changed.  The captures
    only ever held one row at a time, so the bytes between two rows have not
    been observed: rows are found by scanning for their date and time columns
    rather than by walking from one to the next, and a reply whose rows found
    disagree with its row count is withheld (*rows* None) rather than shown in
    part.  A frame is found by the view's title pair, and parsed once the
    whole block is buffered.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[AnnouncementFrame]:
        """Absorb *data* and return every frame now complete."""
        self._buf += data
        out: list[AnnouncementFrame] = []
        while True:
            i = self._buf.find(_ANN_MARKER)
            if i < 0:
                # Keep a tail that may hold the header and the start of a
                # straddling marker.
                del self._buf[:-(len(_ANN_MARKER) - 1 - _ANN_OPCODE_AT)]
                break
            if i + _ANN_OPCODE_AT < 0:
                # The header was cut off before this parser saw it.
                del self._buf[:i + 1]
                continue
            (n, view_id) = struct.unpack_from("<II", self._buf, i + _ANN_LENGTH_AT)
            if not _ANN_COUNT_AT + 4 - _ANN_VIEW_AT <= n <= _MAX_RECORD_LEN:
                # Too short to hold the row count, or too long to be real:
                # the title turned up inside other bytes.
                del self._buf[:i + 1]
                continue
            end = i + _ANN_VIEW_AT + n
            if len(self._buf) < max(end, i + _ANN_COUNT_AT + 4):
                del self._buf[:i + _ANN_OPCODE_AT]
                break
            start = i + _ANN_OPCODE_AT
            opcode = bytes(self._buf[start:start + 2])
            kind = _ANN_KINDS.get(opcode, "changed")
            rows = None
            if kind == "reply":
                rows = _announcement_rows(bytes(self._buf[i:end]), view_id)
            del self._buf[:end]
            out.append(AnnouncementFrame(kind, view_id, rows))
        return out


def _announcement_rows(block: bytes, view_id: int) -> list[dict] | None:
    """Return the rows of one reply, *block* starting at its title pair."""
    (count,) = struct.unpack_from("<I", block, _ANN_COUNT_AT)
    rows = []
    for m in _ANN_ROW.finditer(block, _ANN_COUNT_AT + 4):
        cur = _Cursor(block, m.end())
        try:
            kind = cur.string()
            cur.skip(1)
            text = cur.string(_MAX_ANN_TEXT)
            cur.skip(6)
        except _ShortRecord:
            continue
        try:
            priority = cur.string()
        except _ShortRecord:
            priority = ""
        (ticks,) = struct.unpack("<Q", m.group(1))
        rows.append({
            "text": text,
            "ticks": ticks,
            "date": m.group(2).decode("ascii"),
            "time": m.group(3).decode("ascii"),
            "type": kind,
            "priority": priority,
        })
    if len(rows) != count:
        log.warning(
            "Announcements reply for view %d holds %d rows but %d were read – "
            "withheld: %s", view_id, count, len(rows), block.hex(),
        )
        return None
    return rows


class EntryListReply(NamedTuple):
    """The reply to one entry-list subscription.

    *rows* is *None* when the reply could not be read whole.
    """

    rows: list[dict] | None


class EntryListParser:
    """Parse the reply to one results view subscription out of the ``:51738`` stream.

    Built per subscription for the awaited *view_id*.  The frame has the same
    layout as an Announcements frame (:class:`AnnouncementParser`): a 59-byte
    header with the opcode in its first two bytes, then — integers u32
    little-endian, a ``str`` a u32 length then that many bytes::

        u32   block length    counted from the view id
        u32   view id         the id the client chose when it subscribed
        u32   1
        u32   0
        str   title           "Not classified" in every capture
        str   title           the same again
        u32   row count
        rows

    The title is a classification group label, not the view's name, so the
    frame is found by ``view id, 1, 0`` for the awaited view instead; it is
    taken only with opcode ``24 80`` (the reply — the view's pushes and its
    ``21 80`` column frame are skipped), two equal titles and a plausible
    length.  Each row is a head and a tail, with a middle between them
    holding position, laps, times and status — longer in a finished race,
    and not read::

        head: str number, 1 byte (not 0),
              str first name, 00, str last name, 00, str name, 00, str class, 00
        tail: (str, separator) x 10     CarAdditional0-9: model, capacity, code, …
              str car reg, 00, str driver reg, 00
              u32 n, 1 byte, n x u32    the numeric transponders
              str transponder           as the rMonitor feed carries it

    The ten CarAdditional fields share one separator byte, which is ``0f`` in
    some replies and ``10`` in others — mixed within one reply between its
    finished and DNS rows — so it is never fixed, only required non-zero and
    the same across a row.  Every ``str`` read must hold no byte below
    ``0x20``: a u32 in a finished row's middle can read as a length that runs
    through the real model field's length prefix, and would otherwise anchor
    a tail early.  Rows are found by walking these typed fields — a head,
    then the first tail at or after its end, then the next head after that
    tail — never by string scans keyed on name casing.  Number and class
    must be non-empty, as must the car reg, and a tail must read whole, its
    transponders included.  A reply whose rows found disagree with its row count
    is withheld (*rows* None) rather than forwarded in part.  A transponder
    field holding two, comma-joined, is kept raw.
    """

    def __init__(self, view_id: int) -> None:
        self.view_id = view_id
        self._marker = struct.pack("<III", view_id, 1, 0)
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[EntryListReply]:
        """Absorb *data* and return the reply if it is now complete."""
        self._buf += data
        out: list[EntryListReply] = []
        while True:
            i = self._buf.find(self._marker)
            if i < 0:
                # Keep a tail that may hold the header and the start of a
                # straddling marker.
                del self._buf[:-(len(self._marker) - 1 - _ENT_OPCODE_AT)]
                break
            if i + _ENT_OPCODE_AT < 0:
                # The header was cut off before this parser saw it.
                del self._buf[:i + 1]
                continue
            (n,) = struct.unpack_from("<I", self._buf, i + _ENT_LENGTH_AT)
            start = i + _ENT_OPCODE_AT
            if (
                bytes(self._buf[start:start + 2]) != _ENT_REPLY
                or not _ENT_MIN_BLOCK <= n <= _MAX_RECORD_LEN
            ):
                del self._buf[:i + 1]
                continue
            end = i + n
            if len(self._buf) < end:
                del self._buf[:start]
                break
            block = bytes(self._buf[i:end])
            cur = _Cursor(block, 12)
            try:
                titled = cur.string() == cur.string()
                cur.u32()
            except _ShortRecord:
                titled = False
            if not titled:
                del self._buf[:i + 1]
                continue
            del self._buf[:end]
            out.append(EntryListReply(_entry_list_rows(block, self.view_id, cur.pos)))
        return out


def _entry_list_head(block: bytes, pos: int) -> tuple[list[str], int] | None:
    """Return a row head's five strings at *pos* and the offset after it."""
    cur = _Cursor(block, pos)
    try:
        fields = [cur.printable()]
        if cur.byte() == 0:
            return None
        for _ in range(4):
            fields.append(cur.printable())
            if cur.byte() != 0:
                return None
    except _ShortRecord:
        return None
    if not fields[0] or not fields[4]:
        return None
    return fields, cur.pos


def _entry_list_tail(block: bytes, pos: int) -> tuple[list[str], str, str, int] | None:
    """Return a row tail's CarAdditional fields, car reg and transponder at
    *pos*, and the offset after it."""
    cur = _Cursor(block, pos)
    extra = []
    sep = None
    try:
        for _ in range(_ENT_EXTRA_FIELDS):
            extra.append(cur.printable())
            b = cur.byte()
            if b == 0 or (sep is not None and b != sep):
                return None
            sep = b
        regs = []
        for _ in range(2):
            regs.append(cur.printable())
            if cur.byte() != 0:
                return None
        if not regs[0]:
            # No row on record lacks one, and a row dropped for it would make
            # the list withdraw that entrant's code.
            return None
        n = cur.u32()
        cur.skip(1 + 4 * n)
        transponder = cur.printable()
    except _ShortRecord:
        # A row cut short is no row, so the count check withholds the reply:
        # a list missing a row would withdraw that entrant's code.
        return None
    return extra, regs[0], transponder, cur.pos


def _entry_list_rows(block: bytes, view_id: int, pos: int) -> list[dict] | None:
    """Return the rows of one reply, *block* starting at its view id and its
    rows at *pos*."""
    (count,) = struct.unpack_from("<I", block, pos - 4)
    rows = []
    while pos < len(block):
        head = _entry_list_head(block, pos)
        if head is None:
            pos += 1
            continue
        fields, q = head
        while q < len(block) and (tail := _entry_list_tail(block, q)) is None:
            q += 1
        if q >= len(block):
            break
        extra, car_reg, transponder, pos = tail
        rows.append({
            "entrant_id": car_reg,
            "number": fields[0],
            "class_name": fields[4],
            "transponder": transponder,
            "class_code": extra[_ENT_CODE_FIELD],
        })
    if len(rows) != count:
        # A reply runs to ~23 KB, so its bytes are logged only at debug.
        log.warning(
            "Entry list reply for view %d holds %d rows but %d were read – withheld",
            view_id, count, len(rows),
        )
        log.debug("Withheld entry list reply for view %d: %s", view_id, block.hex())
        return None
    return rows


def _parse_body(body: bytes) -> PushRecord | None:
    # The same decoding RMonitorClient applies to :50000, so the server's
    # exact class-name gate compares like with like.
    text = body.decode("utf-8", errors="replace")
    m = _PUSH_HEADER.match(text)
    if m is None:
        log.debug("Skipping a non-competitor record: %.60r", text)
        return None
    return PushRecord(m.group(1), m.group(2), text[m.end():].split("\t"))


def record_entry(rec: PushRecord) -> dict | None:
    """Return the join fields of *rec*, or *None* if it has no usable code.

    The fields, 0-indexed: 0 entrant id (the Car/Bike Reg, the entrant's
    registration id), 1 Driver Reg (per registration, not per person), 2 car
    number, 3 class name, 4 transponder, 5 ``'0'``, 6 transponder again,
    7 first name, 8 last name, 9 car model, 10 engine capacity, 11 class
    code, 12-18 empty.  Records have been 19 fields in every capture.  The code is taken
    only from field 11 — never derived from the class name, whose mapping to
    codes is many-to-many.
    """
    f = rec.fields
    if len(f) < 12 or not f[0] or not f[11]:
        return None
    return {
        "entrant_id": f[0],
        "kind": rec.kind,
        "number": f[2],
        "class_name": f[3],
        "transponder": f[4],
        "class_code": f[11],
    }


def entry_list_entries(rows: list[dict]) -> list[dict]:
    """Return the join fields of entry-list *rows*, in :func:`record_entry`'s shape.

    A row without a car reg or a code is dropped, and every entry is marked
    ``kind`` ``"entry list"``.  The code is taken only from CarAdditional2,
    the field the host labels "Class" — never derived from the class name.
    """
    return [
        {
            "entrant_id": r["entrant_id"],
            "kind": "entry list",
            "number": r["number"],
            "class_name": r["class_name"],
            "transponder": r["transponder"],
            "class_code": r["class_code"],
        }
        for r in rows
        if r["entrant_id"] and r["class_code"]
    ]


class _ShortRecord(Exception):
    """A registry record ran past the buffer or carried an implausible length."""


class _Cursor:
    """Bounds-checked little-endian reads over one registry record."""

    def __init__(self, buf: bytes, pos: int) -> None:
        self.buf = buf
        self.pos = pos

    def u32(self) -> int:
        if self.pos + 4 > len(self.buf):
            raise _ShortRecord
        (value,) = struct.unpack_from("<I", self.buf, self.pos)
        self.pos += 4
        return value

    def byte(self) -> int:
        if self.pos >= len(self.buf):
            raise _ShortRecord
        self.pos += 1
        return self.buf[self.pos - 1]

    def skip(self, n: int) -> None:
        if self.pos + n > len(self.buf):
            raise _ShortRecord
        self.pos += n

    def printable(self) -> str:
        """Read a ``str`` holding no byte below ``0x20``."""
        start = self.pos + 4
        text = self.string()
        if any(b < 0x20 for b in self.buf[start:self.pos]):
            raise _ShortRecord
        return text

    def string(self, limit: int = _MAX_REGISTRY_STR) -> str:
        n = self.u32()
        if n > limit:
            raise _ShortRecord
        start = self.pos
        self.skip(n)
        # The same decoding as the pushes and the rMonitor feed, so the
        # server's exact class-name gate compares like with like.
        return self.buf[start:self.pos].decode("utf-8", errors="replace")


def parse_registry(buf: bytes) -> list[dict]:
    """Return the competitor registry records found anywhere in *buf*.

    *buf* is the model the host sends in answer to the two send-model
    records.  Each registry record is laid out as below — integers are u32
    little-endian, and a ``str`` is a u32 length then that many bytes::

        str   Car/Bike Reg    the registration id; 1-32 alphanumeric characters
        u32   0
        u32   1
        u32   transponder
        8 bytes
        u32   10
        str   car model
        str   capacity        sometimes free text
        str   class code      may carry a status suffix; taken as-is
        str x6                usually empty
        5 bytes
        str   car number
        str   class name      exact text, as the feed's class description
        8 bytes               zero in every pull measured; not read
        str   Driver Reg      not read
        str   first name      not read
        str   last name       not read
        str   driver code     not read
        ...                   not read

    The Car/Bike Reg is unique within a pull and names one registration,
    which the host edits in place, so its number and class are current.
    Records are found by scanning for the id and the two constants after it,
    so binary around them is skipped; a record that runs past the buffer or
    carries a string longer than 255 bytes is skipped too.  The class code is
    taken only from its own field — never derived from the class name, whose
    mapping to codes is many-to-many.  A record with no class name or
    transponder 0 is dropped.  A record with no code is kept, its code
    ``""``: the server accepts a registry code only when every record in the
    class carries it, and a codeless record is one that does not.  The rest
    are deduplicated on all five fields, not on the id alone: an id is
    unique in every pull measured, and one that ever appeared twice with
    different fields is kept twice, so the server sees the disagreement and
    withholds rather than this parser picking one.  They are returned as
    ``{"registration_id", "number", "transponder", "class_name",
    "class_code"}`` dicts in order of first appearance.
    """
    seen: dict[tuple[str, str, str, str, str], None] = {}
    for m in _REGISTRY_ANCHOR.finditer(buf):
        if m.group(1)[0] != len(m.group(2)):
            continue
        cur = _Cursor(buf, m.end())
        try:
            transponder = cur.u32()
            cur.skip(8)
            if cur.u32() != 10:
                continue
            cur.string()  # car model
            cur.string()  # capacity
            code = cur.string()
            for _ in range(6):
                cur.string()
            cur.skip(5)
            number = cur.string()
            class_name = cur.string()
        except _ShortRecord:
            continue
        if not class_name or transponder == 0:
            continue
        reg_id = m.group(2).decode("ascii")
        seen[(reg_id, number, str(transponder), class_name, code)] = None
    return [
        {
            "registration_id": reg_id,
            "number": number,
            "transponder": tx,
            "class_name": name,
            "class_code": code,
        }
        for reg_id, number, tx, name, code in seen
    ]


def parse_runs(buf: bytes) -> list[dict]:
    """Return the run table records found anywhere in *buf*.

    *buf* is the same model :func:`parse_registry` reads.  Each run record
    is laid out as below, integers u32 little-endian::

        16 bytes              not read: u32 1 plus 12 bytes, 16 zero bytes,
                              or other bytes
        u32   run id          0x4000xxxx
        u32   flags           not read
        u32   group id        0x8000xxxx: the championship the run belongs to
        str   run name

    The record is anchored on its own fields, because the 16 bytes in front
    vary: requiring the ``u32 1`` missed every run written behind zeros
    (#108).  The table spans many past meetings, and run names repeat
    across them — one name can belong to dozens of runs — so a name alone
    rarely picks one run.  A run's name can also be edited after the table
    was pulled.  Ids are returned as ``0x`` plus eight uppercase hex digits,
    the form the pushes' run tags take.  A name longer than 255 bytes or not
    printable is skipped, as is a record running past the buffer.

    A run with an empty name is kept, with ``name`` ``""``, because its
    group still scopes the run's pushes.  With no name to validate it, it is
    kept only behind the ``u32 1`` lead-in, and only for an id no named
    record claims.  The rest are returned as ``{"run_id", "group_id",
    "name"}`` dicts, one per run id, the first named record kept, in order
    of first appearance.
    """
    named: dict[str, dict] = {}
    nameless: dict[str, dict] = {}
    order: dict[str, None] = {}
    for m in _RUN_ANCHOR.finditer(buf):
        (rid,) = struct.unpack("<I", m.group(1))
        (gid,) = struct.unpack("<I", m.group(3))
        cur = _Cursor(buf, m.start() + 12)
        try:
            name = cur.string()
        except _ShortRecord:
            continue
        if not name.isprintable():
            continue
        lead_in = m.start() - 16
        if not name and (lead_in < 0 or buf[lead_in:lead_in + 4] != _RUN_LEAD_IN):
            continue
        run_id = f"0x{rid:08X}"
        record = {"run_id": run_id, "group_id": f"0x{gid:08X}", "name": name}
        (named if name else nameless).setdefault(run_id, record)
        order.setdefault(run_id, None)
    return [named.get(run_id) or nameless[run_id] for run_id in order]


def parse_groups(buf: bytes) -> dict[str, str]:
    """Return ``{group id: name}`` for the group records found in *buf*.

    *buf* is the same model :func:`parse_registry` reads.  A group is an
    Event — the race name a run-state notice ends with — and each run
    record names its group.  Each group record is laid out as below,
    integers u32 little-endian::

        u32   group id        0x8000xxxx
        u8    flag            not read
        u32   unknown         not read
        u32   parent id       0x8000xxxx, or 0xFFFFFFFF for a root group
        str   group name
        8 bytes               zero
        u32   unknown, twice  not read
        u8    unknown         not read

    Only the id and the name are read; the eight zero bytes after the name
    are required, which rejects most false anchors.  The groups arrive only
    in the on-connect pull, spanning many past meetings, so a name repeats
    across meetings but an id is unique.  Ids are returned as ``0x`` plus
    eight uppercase hex digits, as :func:`parse_runs` returns them.  A name
    that is empty, longer than 255 bytes or not printable is skipped, as is
    a record running past the buffer; the first record per id is kept.
    """
    groups: dict[str, str] = {}
    for m in _GROUP_ANCHOR.finditer(buf):
        (gid,) = struct.unpack("<I", m.group(1))
        cur = _Cursor(buf, m.end())
        try:
            name = cur.string()
            if cur.u32() or cur.u32():
                continue
        except _ShortRecord:
            continue
        if not name or not name.isprintable():
            continue
        groups.setdefault(f"0x{gid:08X}", name)
    return groups


def _parse_model(buf: bytes) -> tuple[list[dict], list[dict], dict[str, str]]:
    # All three parses in one worker-thread hop.
    return parse_registry(buf), parse_runs(buf), parse_groups(buf)


async def _backoff_sleep(delay: float) -> None:
    # A seam of its own so tests can record the delays without patching
    # asyncio.sleep for the whole event loop.
    await asyncio.sleep(delay)

class _ModelTooLarge(Exception):
    """The model outgrew :data:`MODEL_BUFFER_CAP` before the stream idled."""


class ClassCodeStatus(NamedTuple):
    """What the class-code client is doing, for a status display.

    *state* is one of ``"disabled"`` (class codes are switched off and the
    client never runs), ``"connecting"``, ``"connected"`` (the handshake is
    complete), ``"retrying"`` (the connection failed: *detail* says why and
    *retry_in* is the reconnect delay) or ``"delivery_failed"`` (a batch or
    preload was not accepted: *retry_in* is the retry delay).  *preloaded*
    is the size of the last delivered registry preload, *pushed* the number
    of entries delivered in pushed batches since the client started, and
    *last_delivery* the wall-clock time of the last delivery of either kind.
    """

    state: str
    preloaded: int = 0
    pushed: int = 0
    last_delivery: float | None = None
    detail: str = ""
    retry_in: float | None = None


class ClassCodeClient:
    """Hold one ``:51738`` connection and forward class codes.

    On every connect, once the handshake is complete, the client pulls the
    host's model and parses its competitor registry (:func:`parse_registry`);
    a complete pull is handed to *on_batch* as one ``class_code_preload``
    message, replacing any preload not yet delivered.  A pull counts as
    complete when the stream has been quiet for *model_idle* seconds — the
    model's own framing is not parsed, and that quiet was measured sufficient
    live.  A pull still running after *model_cap* seconds, or larger than
    :data:`MODEL_BUFFER_CAP`, is not forwarded: the server trusts a registry
    code only when no other record of that class disagrees, which a cut-short
    registry cannot show.  Neither fails the connection.

    Each burst of pushes is deduplicated last-wins per run id and entrant,
    and handed to *on_batch* as one ``class_codes`` message per run id once
    the stream has been quiet for *flush_quiet* seconds.  A batch or preload
    that *on_batch* fails to deliver is kept and retried after
    *retry_initial* seconds, doubling to *retry_max*: pushes follow
    entry-list edits and are not repeated, so a dropped batch would stay
    missing until that entry is edited again.  Entries pushed since the
    failure win over the kept ones, and kept entries outlive a reconnect.
    Every entry — and every preload — carries ``age_seconds``, its time since
    it was read from the socket, so the server dates it from the read rather
    than from a delayed retry's arrival — measured on this process's
    monotonic clock, so the two hosts' clocks need not agree.  Failures of
    every kind are logged and retried after a backoff; :meth:`run` never
    raises anything but cancellation, so it can run beside the ``:50000``
    feed without taking it down.  The timing knobs exist so tests run fast.

    The host also announces on the stream each run it starts
    (:class:`RunStateParser`).  The latest started run is handed to
    *on_batch* as a ``class_code_run`` message, after any preload and before
    the pushes of the same burst, and retried like them; the server uses it
    to keep only the running run's pushes.  A stopped run is not forwarded.

    The client also subscribes to the started run's announcements view
    (:func:`build_view_open`, :class:`AnnouncementParser`) and hands its rows
    to *on_batch* as an ``announcements`` message, delivered after the run
    and retried like it, the latest replacing any not yet delivered.  Only a
    subscription's reply is the truth: a push — even a delete, which still
    carries the deleted row — only says something changed, so
    *announce_resubscribe_delay* seconds after one (a burst coalesces) the
    run is subscribed again on a new view, its reply forwarded and the view
    it replaces closed.  The run is also re-subscribed every
    *announce_refresh_interval* seconds, and every reply forwarded, so a
    restarted server recovers within that; a subscription not answered
    within *announce_reply_timeout* seconds is sent again.  A stop leaves the
    run subscribed — its views, refreshes and entry list alike — until
    another run starts or is picked, or, while picking, a session names no
    run, since race control posts the reason for a stop after it.  The run is
    remembered across a reconnect and re-subscribed once the registry is
    pulled.  A relay started mid-run has seen no start, so until it reads
    one it picks the run by the session's name instead
    (:meth:`note_session`).

    With *entry_list* set, the client also subscribes to the same run's
    results view (:data:`RESULTS_VIEW`, :class:`EntryListParser`) on every
    start, pick and reconnect — not when a picked run's own start notice
    arrives, which keeps the pick's subscription: its reply lists every
    entrant of the run with
    the code the host holds, DNS entrants included.  The reply is taken once
    and the view closed, since an open one streams the whole table about
    once a second; its rows go as a ``class_codes`` message for that run
    marked ``entry_list``, entries of ``kind`` ``"entry list"``, after the
    run and before the pushes of the same burst, and retried like them — the
    latest per run replacing any not yet delivered.  The server takes it as
    the run's whole entry list, replacing the codes of the run's earlier
    ones (``RaceState._class_codes`` in ``server/race_state.py``).  The run is
    subscribed again every *entry_list_refresh_interval* seconds, to catch an
    entry edited mid-run that no push carried, and a subscription not
    answered within *announce_reply_timeout* seconds is closed and sent again
    on the same connection — never on a new one, since every connect raises
    an operator notice.  A reply whose rows disagree with its count forwards
    nothing and waits for the refresh.

    *on_status*, if given, is called with a :class:`ClassCodeStatus` on each
    connect attempt, completed handshake, connection failure, delivery and
    failed delivery; an exception it raises is logged and ignored.

    Delivery runs as a task of its own, never inline in the hold loop:
    *on_batch* may spend minutes retrying an HTTP error, and a hold loop
    waiting on it would send no keepalive, so the timing host would drop the
    connection after ~30 s and the reconnect would cost the operator another
    on-screen notice.  At most one delivery is in flight at a time.
    """

    def __init__(
        self,
        host: str,
        on_batch,
        *,
        on_status=None,
        port: int = PORT,
        keepalive_interval: float = 8.0,
        flush_quiet: float = 0.5,
        reconnect_initial: float = 60.0,
        reconnect_max: float = 900.0,
        first_idle: float = 0.3,
        record_idle: float = 0.15,
        model_idle: float = 1.5,
        model_cap: float = 15.0,
        handshake_cap: float = 30.0,
        retry_initial: float = 5.0,
        retry_max: float = 300.0,
        announce_resubscribe_delay: float = 1.0,
        announce_refresh_interval: float = 60.0,
        announce_reply_timeout: float = 10.0,
        entry_list: bool = True,
        entry_list_refresh_interval: float = 60.0,
    ) -> None:
        self.host = host
        self.port = port
        self.on_batch = on_batch
        self.on_status = on_status if on_status is not None else lambda status: None
        self.keepalive_interval = keepalive_interval
        self.flush_quiet = flush_quiet
        self.reconnect_initial = reconnect_initial
        self.reconnect_max = reconnect_max
        self.first_idle = first_idle
        self.record_idle = record_idle
        # No keepalive goes out while the model is read, and the first one
        # follows a keepalive interval after it: the cap plus that interval
        # must stay well under the host's ~30 s silent-client drop.
        self.model_idle = model_idle
        self.model_cap = model_cap
        self.handshake_cap = handshake_cap
        self.retry_initial = retry_initial
        self.retry_max = retry_max
        self.announce_resubscribe_delay = announce_resubscribe_delay
        self.announce_refresh_interval = announce_refresh_interval
        self.announce_reply_timeout = announce_reply_timeout
        self.entry_list = entry_list
        self.entry_list_refresh_interval = entry_list_refresh_interval
        self._retry_delay = retry_initial
        # Event-loop time before which a failed batch is not re-sent.
        self._retry_at = 0.0
        self._delivery: asyncio.Task | None = None
        self._parser = PushParser()
        self._run_parser = RunStateParser()
        self._pending: dict[str, dict[str, dict]] = {}
        # The last run announced as started and not yet delivered, and the
        # one whose delivery is in flight.
        self._run: dict | None = None
        self._run_in_flight: dict | None = None
        # The preload not yet delivered: its entries and the monotonic time
        # its pull finished.
        self._preload: dict | None = None
        # The run whose announcements are subscribed, and the announcements
        # messages not yet delivered — the latest per run, oldest first;
        # both outlive a reconnect.
        self._ann_run: str | None = None
        self._ann_name = ""
        # The Event last sent for the subscribed run, so its start notice can
        # supply one a pick could not find.
        self._ann_event = ""
        # Names the start the relay read, so the server can tell a restart
        # under the same run id from the start it retired.
        self._ann_start_key = ""
        self._announcements: dict[str, dict] = {}
        # The latest entry list per run not yet delivered, keyed by the
        # lower-cased run id; it outlives a reconnect.
        self._entry_lists: dict[str, dict] = {}
        self._reset_announcement_views()
        # What picks a run by name while no start has been read — the last
        # complete pull's run table and group names, the last $B number and
        # non-95 description, whether a real start has been read, and the
        # lower-cased ids of the runs seen ending meanwhile; all outlive a
        # reconnect.
        self._runs: list[dict] | None = None
        self._groups: dict[str, str] = {}
        self._session_number = ""
        self._session_desc = ""
        self._seen_start = False
        self._ended_runs: set[str] = set()
        self._keepalive = b""
        self._session: dict[str, bytes] = {}
        self._connected = False
        self._preloaded = 0
        self._pushed = 0
        self._last_delivery: float | None = None
        self._last_status = ClassCodeStatus("connecting")

    async def run(self) -> None:
        """Connect, handshake and hold forever, reconnecting after a failure."""
        delay = self.reconnect_initial
        while True:
            writer = None
            survived = False
            reason = "connection closed by the timing host"
            self._parser = PushParser()
            self._run_parser = RunStateParser()
            self._reset_announcement_views()
            try:
                log.info("Connecting to class codes at %s:%s", self.host, self.port)
                self._status("connecting")
                reader, writer = await asyncio.open_connection(self.host, self.port)
                await self._handshake(reader, writer)
                self._connected = True
                self._status("connected")
                await self._pull_registry(reader, writer)
                if self._ann_run is not None:
                    # The views died with the last connection.
                    self._ann_due = asyncio.get_running_loop().time()
                    if self.entry_list:
                        self._ent_due = self._ann_due
                held_from = time.monotonic()
                try:
                    await self._hold(reader, writer)
                finally:
                    survived = time.monotonic() - held_from >= self.keepalive_interval
            except asyncio.CancelledError:
                if self._delivery is not None:
                    self._delivery.cancel()
                await self._close(writer)
                raise
            except (ConnectionError, OSError, HandshakeError, TimeoutError) as exc:
                log.warning("Class-code connection failed: %s", exc)
                reason = str(exc) or type(exc).__name__
            except Exception:
                log.exception("Class-code client: unexpected error – reconnecting after backoff")
                reason = "unexpected error"
            self._connected = False
            await self._close(writer)
            # A burst cut short by a disconnect is still delivered — after any
            # delivery already in flight, never beside it, and not before a
            # failed delivery's retry time; entries held back then go out from
            # the next connection's hold loop once that time has passed.
            try:
                if self._delivery is not None:
                    await self._delivery
                    self._delivery = None
                if self._has_pending() and asyncio.get_running_loop().time() >= self._retry_at:
                    await self._flush()
            except asyncio.CancelledError:
                if self._delivery is not None:
                    self._delivery.cancel()
                raise
            if survived:
                delay = self.reconnect_initial
            log.info("Reconnecting to class codes in %.0fs", delay)
            self._status("retrying", detail=reason, retry_in=delay)
            await _backoff_sleep(delay)
            delay = min(delay * 2, self.reconnect_max)

    def _reset_announcement_views(self) -> None:
        # Per connection: view ids are the client's own, per connection.
        self._ann_parser = AnnouncementParser()
        # The view whose reply was last taken as the truth, the subscription
        # awaiting its reply (view id, loop time sent), the loop time the
        # next subscription is due, and the views to close — the entry
        # list's among them.
        self._ann_view: int | None = None
        self._ann_pending: tuple[int, float] | None = None
        self._ann_due: float | None = None
        self._ann_close: list[int] = []
        self._next_view_id = 1
        # A push read while a subscription awaited its reply: that reply may
        # predate the change, so another subscription follows it.
        self._ann_stale = False
        # The entry-list subscription awaiting its reply (view id, loop time
        # sent, run id), its parser, and the loop time the next one is due.
        self._ent_pending: tuple[int, float, str] | None = None
        self._ent_parser: EntryListParser | None = None
        self._ent_due: float | None = None

    def _drop_announcement_views(self) -> None:
        """Queue every view held or awaited for close."""
        if self._ann_view is not None:
            self._ann_close.append(self._ann_view)
        if self._ann_pending is not None:
            self._ann_close.append(self._ann_pending[0])
        self._ann_view = None
        self._ann_pending = None
        self._ann_stale = False
        self._drop_entry_list_view()

    def _drop_entry_list_view(self) -> None:
        """Queue an awaited entry-list view for close."""
        if self._ent_pending is not None:
            self._ann_close.append(self._ent_pending[0])
        self._ent_pending = None
        self._ent_parser = None

    async def _handshake(self, reader, writer) -> None:
        token, machine = relay_identity()
        async with asyncio.timeout(self.handshake_cap):
            rx = await self._exchange(
                reader, writer, build_record_0(token=token, machine=machine), self.first_idle
            )
            ident = parse_identity_frame(rx)
            if ident is None:
                raise HandshakeError(
                    "no identity frame received – another client may share this "
                    "identity, or the host is not a compatible timing host"
                )
            unit, handle = ident
            self._session = {"token": token, "handle": handle, "unit": unit}
            for template, anchors, tokens in (
                (RECORD_1B, RECORD_1B_ANCHORS, RECORD_1B_TOKENS),
                (RECORD_2B, RECORD_2B_ANCHORS, RECORD_2B_TOKENS),
            ):
                rec = build_session_record(
                    template, **self._session, anchors=anchors, tokens=tokens
                )
                await self._exchange(reader, writer, rec, self.record_idle)
        self._keepalive = build_session_record(
            KEEPALIVE, **self._session,
            anchors=KEEPALIVE_ANCHORS, tokens=KEEPALIVE_TOKENS,
        )
        log.info(
            "Class-code handshake complete: unit %s, handle %s, presented as %s",
            unit.hex(), handle.hex(), machine.decode("ascii"),
        )

    async def _pull_registry(self, reader, writer) -> None:
        # Part of the model answers record 3 and the rest record 4, so every
        # byte read from record 3 on belongs to it — and still goes through
        # _absorb, so a push arriving meanwhile is kept.  So does the rest of
        # a pull cut short, read by the hold loop: pushes interleave with the
        # model, and discarding the tail would lose them.  Model bytes are not
        # mistaken for pushes, because PushParser accepts a record only with
        # its length framing and its "Competitor … [run]:" header — the marker
        # does occur inside a model, but in the pulls measured no such
        # occurrence formed a record.
        model = bytearray()
        try:
            async with asyncio.timeout(self.model_cap):
                for template, anchors, tokens in (
                    (RECORD_3, RECORD_3_ANCHORS, RECORD_3_TOKENS),
                    (RECORD_4, RECORD_4_ANCHORS, RECORD_4_TOKENS),
                ):
                    rec = build_session_record(
                        template, **self._session, anchors=anchors, tokens=tokens
                    )
                    model += await self._exchange(
                        reader, writer, rec, self.record_idle,
                        limit=MODEL_BUFFER_CAP - len(model),
                    )
                model += await self._read_until_idle(
                    reader, self.model_idle, limit=MODEL_BUFFER_CAP - len(model)
                )
        except (TimeoutError, _ModelTooLarge):
            log.warning("Class-code registry pull incomplete – not forwarding a preload")
            return
        pulled_at = time.monotonic()
        entries, runs, groups = await asyncio.to_thread(_parse_model, bytes(model))
        # The run table holds even when the preload is withheld below.
        self._runs = runs
        self._groups = groups
        self._pick_run()
        if not entries and not runs:
            log.warning(
                "Class-code registry pull of %d bytes held no usable records "
                "and no runs – not forwarding a preload", len(model),
            )
            return
        if not entries:
            # The server scopes codes to the running run's group from the run
            # table, and keeps the records it already holds.
            log.warning(
                "Class-code registry pull of %d bytes held no usable records – "
                "forwarding its run table of %d runs only", len(model), len(runs),
            )
        held = self._preload
        records_at = pulled_at
        if not entries and held is not None and held["entries"]:
            # An earlier pull's records still wait to be delivered; they
            # travel with this run table, each part dated as it was read.
            entries, records_at = held["entries"], held["_observed"]
        # Measured as the POST body will be, with an age as wide as a
        # millisecond-rounded one under MAX_ENTRY_AGE can print.
        size = len(json.dumps({
            "type": "class_code_preload", "entries": entries, "runs": runs,
            "age_seconds": MAX_ENTRY_AGE - 0.001,
            "runs_age_seconds": MAX_ENTRY_AGE - 0.001,
        }))
        if size > MAX_PRELOAD_BYTES:
            log.warning(
                "Class-code registry of %d records is %d bytes, over the server's "
                "%d-byte ingest limit – not forwarding a preload",
                len(entries), size, MAX_PRELOAD_BYTES,
            )
            return
        log.info(
            "Class-code registry: %d bytes pulled, %d records with a class, %d runs",
            len(model), len(entries), len(runs),
        )
        self._preload = {
            "entries": entries, "runs": runs,
            "_observed": records_at, "_runs_observed": pulled_at,
        }

    async def _exchange(
        self, reader, writer, record: bytes, idle: float, *, limit: int | None = None
    ) -> bytes:
        writer.write(record)
        await writer.drain()
        return await self._read_until_idle(reader, idle, limit=limit)

    async def _read_until_idle(self, reader, idle: float, *, limit: int | None = None) -> bytes:
        rx = bytearray()
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=idle)
            except TimeoutError:
                return bytes(rx)
            if not data:
                raise ConnectionError("connection closed by the timing host")
            rx += data
            self._absorb(data)
            if limit is not None and len(rx) > limit:
                raise _ModelTooLarge

    async def _hold(self, reader, writer) -> None:
        # No read-silence timeout: pushes follow entry-list edits, so silence
        # is normal, and a reconnect costs the operator an on-screen notice.
        # The keepalive is the liveness mechanism — the server drops a silent
        # client at ~30 s.
        loop = asyncio.get_running_loop()
        next_keepalive = loop.time() + self.keepalive_interval
        last_rx = loop.time()
        while True:
            deadline = next_keepalive
            if self._ann_close:
                deadline = loop.time()
            elif self._ann_pending is not None:
                deadline = min(deadline, self._ann_pending[1] + self.announce_reply_timeout)
            elif self._ann_run is not None and self._ann_due is not None:
                deadline = min(deadline, self._ann_due)
            if self._ent_pending is not None:
                deadline = min(deadline, self._ent_pending[1] + self.announce_reply_timeout)
            elif self.entry_list and self._ann_run is not None and self._ent_due is not None:
                deadline = min(deadline, self._ent_due)
            if self._has_pending():
                if self._delivering():
                    # Re-check once the delivery in flight may have finished.
                    deadline = min(deadline, loop.time() + self.flush_quiet)
                else:
                    deadline = min(deadline, max(last_rx + self.flush_quiet, self._retry_at))
            try:
                data = await asyncio.wait_for(
                    reader.read(65536), timeout=max(0.0, deadline - loop.time())
                )
            except TimeoutError:
                now = loop.time()
                if now >= next_keepalive:
                    writer.write(self._keepalive)
                    await writer.drain()
                    next_keepalive = now + self.keepalive_interval
                self._write_announcement_records(writer, now)
                self._write_entry_list_records(writer, now)
                await writer.drain()
                if (
                    self._has_pending()
                    and not self._delivering()
                    and now - last_rx >= self.flush_quiet
                    and now >= self._retry_at
                ):
                    self._delivery = asyncio.create_task(self._flush())
                continue
            if not data:
                log.warning("Class-code connection closed by remote end")
                return
            self._absorb(data)
            last_rx = loop.time()

    def _write_announcement_records(self, writer, now: float) -> None:
        """Write every queued close, then a due subscription."""
        for view_id in self._ann_close:
            writer.write(build_view_close(self._session, view_id=view_id))
        self._ann_close = []
        if self._ann_run is None:
            return
        if self._ann_pending is not None:
            if now - self._ann_pending[1] < self.announce_reply_timeout:
                return
            # Never answered: close it and try again on a new view.
            log.warning(
                "Announcements subscription on view %d not answered – subscribing again",
                self._ann_pending[0],
            )
            writer.write(build_view_close(self._session, view_id=self._ann_pending[0]))
        elif self._ann_due is None or self._ann_due > now:
            return
        view_id = self._next_view_id
        self._next_view_id += 1
        writer.write(build_view_open(
            self._session, view_id=view_id, name=ANNOUNCEMENTS_VIEW,
            params=[("UniqueID", str(int(self._ann_run, 16)))],
        ))
        log.debug("Subscribed to announcements for run %s on view %d", self._ann_run, view_id)
        self._ann_pending = (view_id, now)
        # The fallback if no reply comes.
        self._ann_due = now + self.announce_refresh_interval

    def _write_entry_list_records(self, writer, now: float) -> None:
        """Write a due entry-list subscription, on this connection only.

        One left unanswered is closed and sent again on a new view of the
        same connection: a request never opens a connection of its own, since
        every connect raises a notice on the timing operator's screen.
        """
        if self._ann_run is None or not self.entry_list:
            return
        if self._ent_pending is not None:
            if now - self._ent_pending[1] < self.announce_reply_timeout:
                return
            log.warning(
                "Entry list subscription on view %d not answered – subscribing again",
                self._ent_pending[0],
            )
            writer.write(build_view_close(self._session, view_id=self._ent_pending[0]))
        elif self._ent_due is None or self._ent_due > now:
            return
        view_id = self._next_view_id
        self._next_view_id += 1
        writer.write(build_view_open(
            self._session, view_id=view_id, name=RESULTS_VIEW,
            params=[*RESULTS_VIEW_PARAMS, ("UniqueID", str(int(self._ann_run, 16)))],
        ))
        log.debug("Subscribed to the entry list for run %s on view %d", self._ann_run, view_id)
        self._ent_pending = (view_id, now, self._ann_run)
        self._ent_parser = EntryListParser(view_id)
        # The fallback if no reply comes.
        self._ent_due = now + self.entry_list_refresh_interval

    def _delivering(self) -> bool:
        return self._delivery is not None and not self._delivery.done()

    def _has_pending(self) -> bool:
        return (
            bool(self._pending)
            or self._preload is not None
            or self._run is not None
            or bool(self._announcements)
            or bool(self._entry_lists)
        )

    def _absorb(self, data: bytes) -> None:
        # The entry list goes first, so a push read in the same chunk wins
        # whichever order they came in: a push after the reply is newer, and
        # one before it carries an edit the reply already holds.
        if self._ent_parser is not None:
            for reply in self._ent_parser.feed(data):
                self._absorb_entry_list(reply)
        for rec in self._parser.feed(data):
            entry = record_entry(rec)
            if entry is None:
                log.debug("Skipping a record without a class code in run %s", rec.run_id)
                continue
            entry["_observed"] = time.monotonic()
            self._pending.setdefault(rec.run_id, {})[entry["entrant_id"]] = entry
        for run in self._run_parser.feed(data):
            if run.state != "started":
                log.debug("Run %s %r is %s", run.run_id, run.name, run.state)
                # A run that stopped before its start was delivered is not
                # running; a newer started run is left alone.  A delivery in
                # flight for it is marked, so it is not retried either.
                # A picked id is the run table's upper-case hex; a notice's
                # may not be.
                stopped_id = run.run_id.lower()
                if self._run is not None and self._run["run_id"].lower() == stopped_id:
                    self._run = None
                flying = self._run_in_flight
                if flying is not None and flying["run_id"].lower() == stopped_id:
                    flying["_stopped"] = True
                # Its views stay open until another run starts or is picked:
                # race control posts the reason for a stop after it.
                if not self._seen_start and (
                    self._ann_run is None or self._ann_run.lower() != stopped_id
                ):
                    # A run seen stopping is never picked, under any later
                    # description: it has ended.  The held pick is recorded
                    # at its session's end instead (_end_session), so a
                    # reconnect's pull before then keeps it subscribed.
                    self._ended_runs.add(stopped_id)
                continue
            log.info("Timing host started run %s %r", run.run_id, run.name)
            picked = (
                not self._seen_start
                and self._ann_run is not None
                and self._ann_run.lower() == run.run_id.lower()
            )
            self._seen_start = True
            if picked:
                # The run picked by name has started: the pick's start key and
                # subscription stand, and refreshes keep naming its start.
                self._correct_event(run.event)
                self._ann_name = run.name
                continue
            self._take_run(run.run_id, run.name, run.event)
        for frame in self._ann_parser.feed(data):
            self._absorb_announcement(frame)

    def _take_run(self, run_id: str, name: str, event: str = "") -> None:
        """Treat *run_id* as the started run: forward it and subscribe.

        *event* is the run's Event name, ``""`` when unknown.
        """
        start_key = uuid.uuid4().hex
        self._run = {
            "run_id": run_id, "name": name, "start_key": start_key,
            "event": event, "_observed": time.monotonic(),
        }
        if run_id != self._ann_run:
            self._drop_announcement_views()
            self._ann_run = run_id
        self._ann_name = name
        self._ann_event = event
        self._ann_start_key = start_key
        # The reply holds the rows that already exist.
        self._ann_due = asyncio.get_running_loop().time()
        if self.entry_list:
            # A start or pick takes a fresh entry list, even of the same run.
            self._drop_entry_list_view()
            self._ent_due = self._ann_due

    def note_session(self, number: str, description: str) -> None:
        """Note the session a feed ``$B`` record names.

        A relay started mid-run has read no start notice, so it would show
        no announcements and leave class codes on the server's name fallback
        until the next run starts.  Until the first real start notice this
        process reads, it picks instead the newest run (largest id) in the
        run table whose name equals the description exactly — the rule of
        the server's ``_class_code_scope`` fallback — and treats it as
        started.  Names repeat across meetings, so a same-named run from
        elsewhere can be picked; that risk is accepted.

        Picking is edge-triggered: only a new session — the number or the
        description changing, as the server's ``_run`` reads it — or a newly
        pulled run table picks, and only while a session is open, never
        after its ``$B,95``.  A session boundary ends the run picked for the
        session it closes; a stop notice ends any other run at once, but
        leaves the pick subscribed until that boundary.  Nothing is picked when
        the newest run of the name has ended, under whatever description —
        an older one of the name is older still; a newer run of the name, in
        a later pull, is picked.

        :param number: the record's session number.
        :param description: the record's session description.
        """
        if number == "95":
            if self._session_number not in ("", "95"):
                self._end_session()
            self._session_number = "95"
            return
        if not description or (number, description) == (
            self._session_number, self._session_desc
        ):
            return
        if self._session_number not in ("", "95"):
            self._end_session()
        self._session_number = number
        self._session_desc = description
        self._pick_run()

    def _end_session(self) -> None:
        # Its views stay open until the next run starts or is picked, or the
        # next session names none: the board still shows the closed session,
        # and its rows with it.
        if not self._seen_start and self._ann_run is not None:
            self._ended_runs.add(self._ann_run.lower())

    def _pick_run(self) -> None:
        if (
            self._seen_start
            or self._session_number in ("", "95")
            or not self._session_desc
            or self._runs is None
        ):
            return
        named = [r for r in self._runs if r["name"] == self._session_desc]
        record = max(named, key=lambda r: int(r["run_id"], 16)) if named else None
        run_id = record["run_id"] if record is not None else None
        if run_id is None or run_id.lower() in self._ended_runs:
            if self._ann_run is not None:
                # Its rows are cleared, so a start for it accepted after
                # all — a delivery in flight — has no rows to show should a
                # later session of its name bind it on the server; marked
                # dropped, so that start shows no race name either.
                self._queue_announcement({
                    "type": "announcements", "run_id": self._ann_run, "rows": [],
                    "stopped": True, "dropped": True,
                    "start_key": self._ann_start_key,
                })
                self._drop_announcement_views()
                self._ann_run = None
                self._ann_due = None
            # While picking, only a pick can be held here or in flight, and
            # a failed delivery of one dropped is not retried either.
            self._run = None
            if self._run_in_flight is not None:
                self._run_in_flight["_stopped"] = True
            return
        event = self._groups.get(record["group_id"], "")
        if self._ann_run is not None and self._ann_run.lower() == run_id.lower():
            # The server ignores a repeat of the run it holds, so a new
            # start key would no longer match its own; a newer pull may
            # still name its event.
            self._correct_event(event)
            return
        log.info(
            "No run start seen – picked run %s %r by name", run_id, self._session_desc
        )
        # A group missing from the pull leaves the event unknown, never guessed.
        self._take_run(run_id, self._session_desc, event)

    def _correct_event(self, event: str) -> None:
        """Resend the subscribed run with *event* if that names a new one.

        The server ignores a repeat of the start it holds except for its
        event, so the pick's start key stands.  An empty *event* never
        blanks one already sent.
        """
        if not event or event == self._ann_event:
            return
        self._run = {
            "run_id": self._ann_run, "name": self._ann_name,
            "start_key": self._ann_start_key, "event": event,
            "_observed": time.monotonic(),
        }
        self._ann_event = event

    def _absorb_announcement(self, frame: AnnouncementFrame) -> None:
        if self._ann_run is None:
            return
        now = asyncio.get_running_loop().time()
        pending = self._ann_pending[0] if self._ann_pending is not None else None
        if frame.kind == "reply":
            if frame.view_id != pending:
                return
            if self._ann_stale:
                # A change was pushed while this reply was awaited, so it may
                # predate the change; the follow-up subscription's reply is
                # forwarded instead.
                log.debug("Withholding a reply that may predate a pushed change")
            elif frame.rows is not None:
                log.info(
                    "Announcements for run %s: %d rows", self._ann_run, len(frame.rows)
                )
                # The name lets a server that lost the started run restore it,
                # and the event its race name.
                msg = {
                    "type": "announcements", "run_id": self._ann_run,
                    "name": self._ann_name, "rows": frame.rows,
                    "start_key": self._ann_start_key,
                }
                if self._ann_event:
                    msg["event"] = self._ann_event
                self._queue_announcement(msg)
            # A withheld reply keeps the rows already forwarded; its view
            # still replaces the held one, so it is closed in turn.
            if self._ann_view is not None:
                self._ann_close.append(self._ann_view)
            self._ann_view = pending
            self._ann_pending = None
            if self._ann_stale:
                self._ann_due = now + self.announce_resubscribe_delay
                self._ann_stale = False
            else:
                self._ann_due = now + self.announce_refresh_interval
        elif frame.view_id in (self._ann_view, pending):
            log.debug("Announcements %s on view %d", frame.kind, frame.view_id)
            if self._ann_pending is not None:
                self._ann_stale = True
                return
            due = now + self.announce_resubscribe_delay
            self._ann_due = due if self._ann_due is None else min(self._ann_due, due)

    def _absorb_entry_list(self, reply: EntryListReply) -> None:
        """Take an entry-list reply once and close its view.

        The view would otherwise stream the whole results table about once a
        second; the next start, pick or refresh subscribes again.
        """
        if self._ent_pending is None:
            return
        view_id, _, run_id = self._ent_pending
        self._ann_close.append(view_id)
        self._ent_pending = None
        self._ent_parser = None
        if self._ann_run is None or run_id != self._ann_run:
            return
        self._ent_due = asyncio.get_running_loop().time() + self.entry_list_refresh_interval
        if reply.rows is None:
            return
        entries = entry_list_entries(reply.rows)
        log.info(
            "Entry list for run %s: %d rows, %d with a code",
            run_id, len(reply.rows), len(entries),
        )
        # The newest replaces any of the run's not yet delivered.
        self._entry_lists.pop(run_id.lower(), None)
        self._entry_lists[run_id.lower()] = {
            "run_id": run_id, "entries": entries, "_observed": time.monotonic(),
        }

    async def _flush(self) -> None:
        pending, self._pending = self._pending, {}
        preload, self._preload = self._preload, None
        failed = False
        if preload is not None:
            failed = not await self._deliver_preload(preload)
        # Taken only now: a stop read while the preload was delivered must
        # still find it pending.
        run, self._run = self._run, None
        if run is not None:
            failed = not await self._deliver_run(run) or failed
        announcements, self._announcements = self._announcements, {}
        kept: dict[str, dict] = {}
        for run_id, msg in announcements.items():
            if not msg.get("stopped") and (
                self._ann_run is None or run_id != self._ann_run.lower()
            ):
                # A reply for a run no longer subscribed: its rows still
                # matter — that run can be the one shown until the session
                # boundary — but marked, so the server never restores or
                # renews the run from it.
                msg = msg | {"superseded": True}
            if not await self._deliver_announcement(msg):
                failed = True
                # Unless a newer message for the run was read meanwhile.
                if run_id not in self._announcements:
                    kept[run_id] = msg
        # Kept ones go back ahead of any read meanwhile, which are newer.
        self._announcements = kept | self._announcements
        # Before the pushes, so a push of the same flush is the newer on the
        # server.
        entry_lists, self._entry_lists = self._entry_lists, {}
        # Runs whose list failed: their pushes wait for it, so a retried list
        # never lands after a newer push and overwrites it on the server.
        deferred: set[str] = set()
        # Runs whose list was delivered, and when it was read: the run's
        # pushes read before it — kept from a failed delivery — are older than
        # the list, which already holds their edits, so they are dropped
        # rather than sent after it to overwrite it.
        listed: dict[str, float] = {}
        for key, held in entry_lists.items():
            if not await self._deliver_entry_list(held):
                failed = True
                deferred.add(key)
                # Unless a newer one for the run was read meanwhile.
                self._entry_lists.setdefault(key, held)
            else:
                listed[key] = held["_observed"]
        for run_id, by_entrant in pending.items():
            if run_id.lower() in deferred:
                kept = self._pending.setdefault(run_id, {})
                for entrant_id, entry in by_entrant.items():
                    kept.setdefault(entrant_id, entry)
                continue
            if run_id.lower() in listed:
                # A push read in the same chunk as the reply is stamped after
                # it, so a tie is a newer push and is kept.
                older = [
                    k for k, e in by_entrant.items()
                    if e["_observed"] < listed[run_id.lower()]
                ]
                for k in older:
                    del by_entrant[k]
                if older:
                    log.debug(
                        "Dropping %d class codes for run %s read before its entry list",
                        len(older), run_id,
                    )
            now = time.monotonic()
            expired = [
                k for k, e in by_entrant.items() if now - e["_observed"] > MAX_ENTRY_AGE
            ]
            if expired:
                log.warning(
                    "Dropping %d undelivered class codes for run %s – older than %.0fh",
                    len(expired), run_id, MAX_ENTRY_AGE / 3600,
                )
                for k in expired:
                    del by_entrant[k]
            if not by_entrant:
                continue
            msg = {
                "type": "class_codes",
                "run_id": run_id,
                "entries": [
                    {k: v for k, v in e.items() if k != "_observed"}
                    | {"age_seconds": round(now - e["_observed"], 3)}
                    for e in by_entrant.values()
                ],
            }
            log.info("Forwarding %d class codes for run %s", len(by_entrant), run_id)
            try:
                await self.on_batch(msg)
            except Exception:
                log.exception("Could not forward class codes for run %s", run_id)
                failed = True
                kept = self._pending.setdefault(run_id, {})
                for entrant_id, entry in by_entrant.items():
                    kept.setdefault(entrant_id, entry)
            else:
                self._pushed += len(by_entrant)
                self._delivered()
        loop = asyncio.get_running_loop()
        if failed:
            self._retry_at = loop.time() + self._retry_delay
            log.warning("Retrying class-code delivery in %.0fs", self._retry_delay)
            self._status("delivery_failed", retry_in=self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, self.retry_max)
        else:
            self._retry_at = 0.0
            self._retry_delay = self.retry_initial

    async def _deliver_preload(self, preload: dict) -> bool:
        """Hand *preload* to *on_batch*; return whether nothing is left to retry.

        Its records and its run table can come from different pulls, so each
        carries its own age — ``age_seconds`` and ``runs_age_seconds`` — and
        each is dropped alone once too old: stale records never take a fresh
        run table with them.
        """
        now = time.monotonic()
        age = now - preload["_observed"]
        runs_age = now - preload.get("_runs_observed", preload["_observed"])
        entries, runs = preload["entries"], preload.get("runs", [])
        stale = entries and age > MAX_ENTRY_AGE
        if stale:
            entries = []
        if runs and runs_age > MAX_ENTRY_AGE:
            runs = []
        if stale or not (entries or runs):
            log.warning(
                "Dropping an undelivered class-code %s – older than %.0fh",
                "preload" if not (entries or runs) else "registry",
                MAX_ENTRY_AGE / 3600,
            )
        if not (entries or runs):
            return True
        preload = preload | {"entries": entries, "runs": runs}
        log.info("Forwarding a class-code preload of %d records", len(entries))
        try:
            await self.on_batch({
                "type": "class_code_preload",
                "entries": entries,
                "runs": runs,
                # A run table alone is dated by its own age, as a server that
                # predates runs_age_seconds reads only this one.
                "age_seconds": round(age if entries else runs_age, 3),
                "runs_age_seconds": round(runs_age, 3),
            })
        except Exception:
            log.exception("Could not forward the class-code preload")
            self._preload = preload
            return False
        # A run table alone leaves the server's records as they were.
        if entries:
            self._preloaded = len(entries)
        self._delivered()
        return True

    async def _deliver_run(self, run: dict) -> bool:
        """Hand the started *run* to *on_batch*; return whether nothing is left to retry.

        A failed delivery is kept for the retry unless a newer run has been
        announced meanwhile, which replaces it, or the run has stopped — or,
        for a run picked by name, the pick has been dropped.
        """
        age = time.monotonic() - run["_observed"]
        if age > MAX_ENTRY_AGE:
            log.warning(
                "Dropping an undelivered run state – older than %.0fh", MAX_ENTRY_AGE / 3600
            )
            return True
        self._run_in_flight = run
        msg = {
            "type": "class_code_run",
            "run_id": run["run_id"],
            "name": run["name"],
            "start_key": run["start_key"],
            "age_seconds": round(age, 3),
        }
        # Absent means unknown, as an older relay sends it.
        if run.get("event"):
            msg["event"] = run["event"]
        try:
            await self.on_batch(msg)
        except Exception:
            log.exception("Could not forward the started run %s", run["run_id"])
            if self._run is None and not run.get("_stopped"):
                self._run = run
            return False
        finally:
            self._run_in_flight = None
        self._delivered()
        return True

    async def _deliver_entry_list(self, held: dict) -> bool:
        """Hand an entry list to *on_batch*; return whether nothing is left to retry.

        It goes as a ``class_codes`` message marked ``entry_list``, which the
        server takes as the run's whole entry list: it replaces the entries
        of the run's earlier lists, so a code cleared on the host, or an
        entrant removed, is withdrawn rather than kept until it expires.
        """
        age = time.monotonic() - held["_observed"]
        if age > MAX_ENTRY_AGE:
            log.warning(
                "Dropping an undelivered entry list for run %s – older than %.0fh",
                held["run_id"], MAX_ENTRY_AGE / 3600,
            )
            return True
        entries = held["entries"]
        log.info("Forwarding an entry list of %d class codes for run %s",
                 len(entries), held["run_id"])
        try:
            await self.on_batch({
                "type": "class_codes",
                "run_id": held["run_id"],
                "entry_list": True,
                # The list's own age too, as a list with no rows has none.
                "age_seconds": round(age, 3),
                "entries": [e | {"age_seconds": round(age, 3)} for e in entries],
            })
        except Exception:
            log.exception("Could not forward the entry list for run %s", held["run_id"])
            return False
        # Not counted as pushed: every refresh resends the whole list.
        self._delivered()
        return True

    def _queue_announcement(self, msg: dict) -> None:
        """Queue *msg*, replacing any for its run, as the newest.

        Kept per run, so a clear for one run never displaces another run's
        message; delivered oldest first, so the server ends on the newest.
        The queue is keyed by the lower-cased run id.
        """
        # Keyed without case: a notice's id may be lower-case hex where a
        # picked run's, from the run table, is upper-case.
        key = msg["run_id"].lower()
        self._announcements.pop(key, None)
        self._announcements[key] = msg

    async def _deliver_announcement(self, msg: dict) -> bool:
        """Hand the announcements *msg* to *on_batch*; return whether it was delivered.

        A reply for the subscribed start carries its event as known now, not
        as queued: a picked run's start notice can correct the event after a
        reply was queued or failed, and the server takes a refresh's event.
        """
        if (
            not msg.get("stopped")
            and not msg.get("superseded")
            and self._ann_run is not None
            and msg["run_id"].lower() == self._ann_run.lower()
            and msg.get("start_key") == self._ann_start_key
        ):
            msg = {k: v for k, v in msg.items() if k != "event"}
            if self._ann_event:
                msg["event"] = self._ann_event
        try:
            await self.on_batch(msg)
        except Exception:
            log.exception("Could not forward the announcements for run %s", msg["run_id"])
            return False
        self._delivered()
        return True

    def _delivered(self) -> None:
        self._last_delivery = time.time()
        # Delivered after a disconnect, it is reported by the "retrying"
        # status that follows, which carries the new counts.
        if self._connected:
            self._status("connected")

    def _status(self, state: str, *, detail: str = "", retry_in: float | None = None) -> None:
        self._last_status = ClassCodeStatus(
            state, self._preloaded, self._pushed, self._last_delivery, detail, retry_in
        )
        try:
            self.on_status(self._last_status)
        except Exception:
            log.exception("Class-code status callback failed")

    @staticmethod
    async def _close(writer) -> None:
        if writer is not None and not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
