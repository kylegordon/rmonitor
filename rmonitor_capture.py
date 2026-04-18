#!/usr/bin/env python3
"""rMonitor diagnostic capture tool.

Connects to RMONITOR_HOST:RMONITOR_PORT and logs every raw rMonitor line
(with a UTC timestamp prefix) to a timestamped file in CAPTURE_DIR.

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
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

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


async def _feed_task(host: str, port: int, log_file) -> None:
    """Connect to the rMonitor TCP feed and write every raw line to *log_file*."""
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
    print(f"  Feed   : {RMONITOR_HOST}:{RMONITOR_PORT}")
    print(f"  Output : {filename}")
    print("  Type an annotation and press Enter to mark it in the log.")
    print("  Press Ctrl-C to stop.\n")

    with open(filename, "w") as log_file:
        _log(log_file, f"# rMonitor capture started at {_ts()}")
        _log(log_file, f"# Feed: {RMONITOR_HOST}:{RMONITOR_PORT}")

        feed = asyncio.create_task(_feed_task(RMONITOR_HOST, RMONITOR_PORT, log_file))
        annotations = asyncio.create_task(_annotation_task(log_file))

        try:
            await asyncio.gather(feed, annotations)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            feed.cancel()
            annotations.cancel()
            await asyncio.gather(feed, annotations, return_exceptions=True)
            _log(log_file, f"# Capture ended at {_ts()}")
            print(f"\n[{_ts()}] Capture saved to {filename}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCapture stopped.")
