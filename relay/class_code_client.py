"""Async client for the class codes on the timing host's secondary port.

The rMonitor feed on port 50000 carries a class number and a free-text class
description, but no class code.  The same timing host serves a second port,
``:51738``, that pushes competitor records carrying the code as a discrete
field.  Its record layout was learned by packet capture on an open network;
this module is a clean-room implementation of the client side.

- **Pushes come only when the timing host loads a run** — a session, a race,
  a restart after a red flag.  Between run loads the connection is silent,
  and that silence is normal, not a failure.
- **The handshake is three records**, sent in lockstep: each one is written
  and the socket read until it idles before the next goes out.  The session
  handle and unit id in the second and third are read from the server's
  identity frame on every connect, never replayed.
- **Every connect raises a visible notice on the timing operator's screen.**
  So there is no read-silence timeout, a connection is only replaced after it
  fails, and the reconnect delay is long and grows.

Records are deduplicated per run id and entrant and forwarded in bursts as
``class_codes`` messages carrying the raw join fields; the server does the
join.  Nothing here ever derives a code from a class name.
"""

from __future__ import annotations

import asyncio
import functools
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
KEEPALIVE_ANCHORS = (4,)
KEEPALIVE_TOKENS = (22,)
IP_SLOT = 71

_IDENTITY_FRAME_PREFIX = b"\x00\x00\x01\x04"

_PUSH_MARKER = b"datamanager"
# A length beyond this is not a real record: the marker literal turned up
# inside binary bytes, so the scan skips past it.
_MAX_RECORD_LEN = 1_000_000
_PUSH_HEADER = re.compile(
    r"^Competitor (added|automatically added|modified) \[([^\]]*)\]:"
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
    *token* is written at each token offset.  Used for records 1B and 2B and
    for the keepalive.
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

    The fields, 0-indexed: 0 entrant id, 1 a second id, 2 car number,
    3 class name, 4 transponder, 5 ``'0'``, 6 transponder again, 7 first
    name, 8 last name, 9 car model, 10 engine capacity, 11 class code, 12-18
    empty.  Records have been 19 fields in every capture.  The code is taken
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


async def _backoff_sleep(delay: float) -> None:
    # A seam of its own so tests can record the delays without patching
    # asyncio.sleep for the whole event loop.
    await asyncio.sleep(delay)


class ClassCodeClient:
    """Hold one ``:51738`` connection and forward pushed class codes.

    Each burst of pushes is deduplicated last-wins per run id and entrant,
    and handed to *on_batch* as one ``class_codes`` message per run id once
    the stream has been quiet for *flush_quiet* seconds.  A batch that
    *on_batch* fails to deliver is kept and retried after *retry_initial*
    seconds, doubling to *retry_max*: a roster is pushed once per run load,
    so a dropped batch would stay missing for the rest of that run.  Entries
    pushed since the failure win over the kept ones, and kept entries outlive
    a reconnect.  Every entry carries ``age_seconds``, its time since it was
    read from the socket, so the server dates it from the push rather than
    from a delayed retry's arrival — measured on this process's monotonic
    clock, so the two hosts' clocks need not agree.  Failures of every kind are logged and retried after a
    backoff; :meth:`run` never raises anything but cancellation, so it can
    run beside the ``:50000`` feed without taking it down.  The timing knobs
    exist so tests run fast.
    """

    def __init__(
        self,
        host: str,
        on_batch,
        *,
        port: int = PORT,
        keepalive_interval: float = 8.0,
        flush_quiet: float = 0.5,
        reconnect_initial: float = 60.0,
        reconnect_max: float = 900.0,
        first_idle: float = 0.3,
        record_idle: float = 0.15,
        tail_idle: float = 0.6,
        handshake_cap: float = 30.0,
        retry_initial: float = 5.0,
        retry_max: float = 300.0,
    ) -> None:
        self.host = host
        self.port = port
        self.on_batch = on_batch
        self.keepalive_interval = keepalive_interval
        self.flush_quiet = flush_quiet
        self.reconnect_initial = reconnect_initial
        self.reconnect_max = reconnect_max
        self.first_idle = first_idle
        self.record_idle = record_idle
        self.tail_idle = tail_idle
        self.handshake_cap = handshake_cap
        self.retry_initial = retry_initial
        self.retry_max = retry_max
        self._retry_delay = retry_initial
        # Event-loop time before which a failed batch is not re-sent.
        self._retry_at = 0.0
        self._parser = PushParser()
        self._pending: dict[str, dict[str, dict]] = {}
        self._keepalive = b""

    async def run(self) -> None:
        """Connect, handshake and hold forever, reconnecting after a failure."""
        delay = self.reconnect_initial
        while True:
            writer = None
            survived = False
            self._parser = PushParser()
            try:
                log.info("Connecting to class codes at %s:%s", self.host, self.port)
                reader, writer = await asyncio.open_connection(self.host, self.port)
                await self._handshake(reader, writer)
                held_from = time.monotonic()
                try:
                    await self._hold(reader, writer)
                finally:
                    survived = time.monotonic() - held_from >= self.keepalive_interval
            except asyncio.CancelledError:
                await self._close(writer)
                raise
            except (ConnectionError, OSError, HandshakeError, TimeoutError) as exc:
                log.warning("Class-code connection failed: %s", exc)
            except Exception:
                log.exception("Class-code client: unexpected error – reconnecting after backoff")
            # A burst cut short by a disconnect is still delivered.
            await self._flush()
            await self._close(writer)
            if survived:
                delay = self.reconnect_initial
            log.info("Reconnecting to class codes in %.0fs", delay)
            await _backoff_sleep(delay)
            delay = min(delay * 2, self.reconnect_max)

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
            for template, anchors, tokens in (
                (RECORD_1B, RECORD_1B_ANCHORS, RECORD_1B_TOKENS),
                (RECORD_2B, RECORD_2B_ANCHORS, RECORD_2B_TOKENS),
            ):
                rec = build_session_record(
                    template, token=token, handle=handle, unit=unit,
                    anchors=anchors, tokens=tokens,
                )
                await self._exchange(reader, writer, rec, self.record_idle)
            await self._read_until_idle(reader, self.tail_idle)
        self._keepalive = build_session_record(
            KEEPALIVE, token=token, handle=handle, unit=unit,
            anchors=KEEPALIVE_ANCHORS, tokens=KEEPALIVE_TOKENS,
        )
        log.info(
            "Class-code handshake complete: unit %s, handle %s, presented as %s",
            unit.hex(), handle.hex(), machine.decode("ascii"),
        )

    async def _exchange(self, reader, writer, record: bytes, idle: float) -> bytes:
        writer.write(record)
        await writer.drain()
        return await self._read_until_idle(reader, idle)

    async def _read_until_idle(self, reader, idle: float) -> bytes:
        rx = bytearray()
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=idle)
            except TimeoutError:
                return bytes(rx)
            if not data:
                raise ConnectionError("closed during handshake")
            rx += data
            self._absorb(data)

    async def _hold(self, reader, writer) -> None:
        # No read-silence timeout: silence between run loads is normal, and a
        # reconnect costs the operator an on-screen notice.  The keepalive is
        # the liveness mechanism — the server drops a silent client at ~30 s.
        loop = asyncio.get_running_loop()
        next_keepalive = loop.time() + self.keepalive_interval
        last_rx = loop.time()
        while True:
            deadline = next_keepalive
            if self._pending:
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
                if (
                    self._pending
                    and now - last_rx >= self.flush_quiet
                    and now >= self._retry_at
                ):
                    await self._flush()
                continue
            if not data:
                log.warning("Class-code connection closed by remote end")
                return
            self._absorb(data)
            last_rx = loop.time()

    def _absorb(self, data: bytes) -> None:
        for rec in self._parser.feed(data):
            entry = record_entry(rec)
            if entry is None:
                log.debug("Skipping a record without a class code in run %s", rec.run_id)
                continue
            entry["_observed"] = time.monotonic()
            self._pending.setdefault(rec.run_id, {})[entry["entrant_id"]] = entry

    async def _flush(self) -> None:
        pending, self._pending = self._pending, {}
        failed = False
        for run_id, by_entrant in pending.items():
            now = time.monotonic()
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
        loop = asyncio.get_running_loop()
        if failed:
            self._retry_at = loop.time() + self._retry_delay
            log.warning("Retrying class-code delivery in %.0fs", self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, self.retry_max)
        else:
            self._retry_at = 0.0
            self._retry_delay = self.retry_initial

    @staticmethod
    async def _close(writer) -> None:
        if writer is not None and not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
