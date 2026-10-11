#!/usr/bin/env python3
"""rMonitor diagnostic capture tool.

Captures both ports of the timing host, always:

- RMONITOR_HOST:RMONITOR_PORT, the rMonitor text feed: every raw line, with a
  UTC timestamp prefix, to ``capture_<stamp>.log`` in CAPTURE_DIR.
- RMONITOR_HOST:51738, the Orbits class-code and results port.  It answers
  only a client, so the relay's ``ClassCodeClient`` connects, following the
  running run by the feed's ``$B``, and every byte it reads and writes goes to
  ``capture_<stamp>_51738_in.bin`` and ``_out.bin``.
  ``capture_<stamp>_51738.jsonl`` indexes those bytes (time, direction,
  offset, length) and records each message the relay would post and each
  connection status; the statuses also go to the main log.  Each connect to
  :51738 raises a notice on the timing operator's screen.

Type an annotation at any time and press Enter to insert a marked entry into
the log — useful for noting session transitions such as "paid practice",
"qualifying" or "race".

Usage:
    python rmonitor_capture.py

Environment variables:
    RMONITOR_HOST   rMonitor feed hostname or IP  (default: 127.0.0.1)
    RMONITOR_PORT   rMonitor feed TCP port         (default: 50000)
    CAPTURE_DIR     Directory for output files     (default: captures)
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from relay.class_code_client import ClassCodeClient
from relay.rmonitor_client import parse_line

RMONITOR_HOST = os.environ.get("RMONITOR_HOST", "127.0.0.1")
RMONITOR_PORT = int(os.environ.get("RMONITOR_PORT", "50000"))
CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", "captures"))

RECONNECT_DELAY = 5.0


def _ts() -> str:
    """Return the current UTC time as an ISO-8601 string (millisecond precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _log(log_file, line: str) -> None:
    """Write *line* (without a trailing newline) to *log_file*, then flush."""
    log_file.write(line + "\n")
    log_file.flush()


async def _feed_task(host: str, port: int, log_file, on_line=lambda line: None) -> None:
    """Connect to the rMonitor TCP feed and write every raw line to *log_file*.

    *on_line* is also called with each line.
    """
    while True:
        try:
            print(f"[{_ts()}] Connecting to {host}:{port} …", flush=True)
            reader, writer = await asyncio.open_connection(host, port)
            _log(log_file, f"# CONNECTED {host}:{port} at {_ts()}")
            print(f"[{_ts()}] Connected.", flush=True)
            try:
                while True:
                    raw = await reader.readline()
                    if not raw:
                        print(f"[{_ts()}] Connection closed by remote end.", flush=True)
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    _log(log_file, f"{_ts()} {line}")
                    on_line(line)
            finally:
                if not writer.is_closing():
                    writer.close()
                    await writer.wait_closed()
                _log(log_file, f"# DISCONNECTED at {_ts()}")
        except (ConnectionError, OSError) as exc:
            msg = f"Connection error: {exc} – retrying in {RECONNECT_DELAY}s"
            print(f"[{_ts()}] {msg}", flush=True)
            _log(log_file, f"# {msg}")
        except asyncio.CancelledError:
            return
        await asyncio.sleep(RECONNECT_DELAY)


class OrbitsCapture:
    """Record the :51738 traffic of a ``ClassCodeClient`` under *base*.

    *base* is the main log's path without its suffix; the files written are
    named in this module's docstring.
    """

    def __init__(self, base: Path, log_file) -> None:
        self._log_file = log_file
        self._raw = {
            "in": open(f"{base}_51738_in.bin", "wb"),
            "out": open(f"{base}_51738_out.bin", "wb"),
        }
        self._index = open(f"{base}_51738.jsonl", "w")

    def _record(self, entry: dict) -> None:
        self._index.write(json.dumps({"at": _ts(), **entry}, default=str) + "\n")
        self._index.flush()

    def on_raw(self, direction: str, data: bytes) -> None:
        f = self._raw[direction]
        self._record({"dir": direction, "offset": f.tell(), "len": len(data)})
        f.write(data)
        f.flush()

    async def on_batch(self, msg: dict) -> None:
        self._record({"message": msg})

    def on_status(self, status) -> None:
        self._record({"status": status._asdict()})
        detail = f" {status.detail}" if status.detail else ""
        _log(self._log_file, f"# 51738 {status.state}{detail} at {_ts()}")

    def close(self) -> None:
        for f in (*self._raw.values(), self._index):
            f.close()


def _session_hook(client: ClassCodeClient):
    """Return an *on_line* that lets *client* pick the running run by name."""
    def on_line(line: str) -> None:
        msg = parse_line(line)
        if msg is not None and msg.get("type") == "run":
            client.note_session(msg.get("unique_number", ""), msg.get("description", ""))
    return on_line


async def _annotation_task(log_file) -> None:
    """Read annotation lines from stdin and write them to *log_file*."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            # run_in_executor keeps the event loop free while waiting for input
            raw = await loop.run_in_executor(None, sys.stdin.readline)
        except asyncio.CancelledError:
            return
        if not raw:
            # EOF – stdin was closed
            return
        annotation = raw.strip()
        if annotation:
            entry = f"# ANNOTATION [{_ts()}]: {annotation}"
            _log(log_file, entry)
            print(f"  ↳ Annotated: {annotation}", flush=True)


async def main() -> None:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    filename = CAPTURE_DIR / f"capture_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log"

    print("rMonitor Capture Tool")
    print(f"  Feed   : {RMONITOR_HOST}:{RMONITOR_PORT} and :51738")
    print(f"  Output : {filename}")
    print("  Type an annotation and press Enter to mark it in the log.")
    print("  Press Ctrl-C to stop.\n")

    with open(filename, "w") as log_file:
        _log(log_file, f"# rMonitor capture started at {_ts()}")
        _log(log_file, f"# Feed: {RMONITOR_HOST}:{RMONITOR_PORT}")
        _log(log_file, f"# Orbits: {RMONITOR_HOST}:51738 to {filename.stem}_51738.*")

        orbits = OrbitsCapture(filename.with_suffix(""), log_file)
        client = ClassCodeClient(
            RMONITOR_HOST, orbits.on_batch, on_status=orbits.on_status, on_raw=orbits.on_raw
        )
        tasks = [
            asyncio.create_task(
                _feed_task(RMONITOR_HOST, RMONITOR_PORT, log_file, _session_hook(client))
            ),
            asyncio.create_task(client.run()),
            asyncio.create_task(_annotation_task(log_file)),
        ]

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            orbits.close()
            _log(log_file, f"# Capture ended at {_ts()}")
            print(f"\n[{_ts()}] Capture saved to {filename}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCapture stopped.")
