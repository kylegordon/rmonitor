"""Tests for rmonitor_capture.py, the diagnostic capture tool."""

import asyncio
import io
import json

import rmonitor_capture
from relay.class_code_client import ClassCodeStatus


def test_orbits_capture_writes_raw_bytes_and_indexes_them(tmp_path):
    log = io.StringIO()
    cap = rmonitor_capture.OrbitsCapture(tmp_path / "capture_x", log)
    cap.on_raw("out", b"hello")
    cap.on_raw("in", b"ab")
    cap.on_raw("in", b"cde")
    asyncio.run(cap.on_batch({"type": "class_codes", "run_id": "4000282B"}))
    cap.on_status(ClassCodeStatus("connected"))
    cap.close()

    assert (tmp_path / "capture_x_51738_in.bin").read_bytes() == b"abcde"
    assert (tmp_path / "capture_x_51738_out.bin").read_bytes() == b"hello"
    index = [
        json.loads(line)
        for line in (tmp_path / "capture_x_51738.jsonl").read_text().splitlines()
    ]
    chunks = [(e["dir"], e["offset"], e["len"]) for e in index if "dir" in e]
    assert chunks == [("out", 0, 5), ("in", 0, 2), ("in", 2, 3)]
    assert {"type": "class_codes", "run_id": "4000282B"} in [e.get("message") for e in index]
    assert index[-1]["status"]["state"] == "connected"
    assert "# 51738 connected" in log.getvalue()


def test_session_hook_passes_each_run_line_to_the_class_code_client():
    class Client:
        def __init__(self):
            self.sessions = []

        def note_session(self, unique_number, description):
            self.sessions.append((unique_number, description))

    client = Client()
    on_line = rmonitor_capture._session_hook(client)
    on_line('$B,7,"Race 12 - 1st Race"')
    on_line('$F,2,"00:00:00","12:15:29","00:10:27","Green "')
    assert client.sessions == [("7", "Race 12 - 1st Race")]
