"""Integration test: replay a sample capture file through the parser and state engine."""

import pathlib

import pytest

from app.race_state import RaceState
from app.rmonitor_client import parse_line

EXAMPLES_DIR = pathlib.Path(__file__).resolve().parent.parent / "examples"
SAMPLE_FILES = sorted(EXAMPLES_DIR.glob("*.txt"))


@pytest.mark.parametrize(
    "sample_file",
    SAMPLE_FILES,
    ids=[f.name for f in SAMPLE_FILES],
)
def test_replay_sample_file(sample_file):
    """Replay every line in a sample file through parse_line → RaceState.

    Verifies that:
    - No exceptions are raised during parsing or state processing
    - At least some competitors are registered
    - The snapshot is serialisable and well-formed
    """
    state = RaceState()
    parsed_count = 0

    with open(sample_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            msg = parse_line(line)
            if msg:
                state.process(msg)
                parsed_count += 1

    assert parsed_count > 0, f"No messages parsed from {sample_file.name}"
    assert len(state.competitors) > 0, f"No competitors after replaying {sample_file.name}"

    snap = state.snapshot()
    assert isinstance(snap, dict)
    assert "entries" in snap
    assert len(snap["entries"]) == len(state.competitors)

    # Verify every entry has required fields
    for entry in snap["entries"]:
        assert "reg_number" in entry
        assert "number" in entry
        assert "class_description" in entry
