"""Tests for rMonitor protocol parsing.

Message formats are verified against the protocol as implemented by:
  - https://github.com/only-entertainment/rmonitor
  - https://github.com/zacharyfox/RMonitorLeaderboard
"""

import pathlib
import re

import pytest

from relay.rmonitor_client import parse_line

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Every committed feed sample, named rather than globbed.  `captures/*.log` is
#: gitignored and these two are force-added past it, so a glob would sweep in
#: whatever local captures a developer happens to hold and make these tests mean
#: something different here than in CI.
FIXTURE_FILES = [
    _REPO_ROOT / "examples" / "2009 Sebring Test ALMS Session 4 - 0800-1000.txt",
    _REPO_ROOT / "examples" / "2009 Sebring Test ALMS Session 5 - 1410-1620.txt",
    _REPO_ROOT / "examples" / "2009 Sebring Test Lites Session 4 - 1010-1200.txt",
    _REPO_ROOT / "captures" / "capture_20260418T132655.log",
    _REPO_ROOT / "captures" / "capture_20260912T143259-caution-excerpt.log",
]


def test_every_named_fixture_is_present():
    """The corpus the derived tests measure is committed and complete.

    Without this, a renamed or dropped sample would quietly shrink what those
    tests examine instead of failing.
    """
    missing = [p.name for p in FIXTURE_FILES if not p.is_file()]
    assert not missing, f"committed fixtures missing: {missing}"

# $F,laps_to_go,"time_to_go","time_of_day","race_time","flag" — the flag field
# raw, before :func:`_tokenize` strips it.
_F_FLAG_FIELD = re.compile(r'\$F,[^,]*,"[^"]*","[^"]*","[^"]*","([^"]*)"')
# $B,unique_number,"description" — the run number, 95 being the end sentinel.
_B_RUN_NUMBER = re.compile(r'\$B,([^,]*),"')
_B_RUN_RECORD = re.compile(r'\$B,([^,]*),"([^"]*)"')


# -- $F Heartbeat -----------------------------------------------------------

def test_heartbeat():
    msg = parse_line('$F,14,"00:12:45","13:34:23","00:09:47","Green "')
    assert msg is not None
    assert msg["type"] == "heartbeat"
    assert msg["laps_to_go"] == "14"
    assert msg["time_to_go"] == "00:12:45"
    assert msg["time_of_day"] == "13:34:23"
    assert msg["race_time"] == "00:09:47"
    assert msg["flag"] == "Green"


def test_heartbeat_flag_trim():
    msg = parse_line('$F,9999,"00:00:00","07:59:59","00:00:00","Green "')
    assert msg["flag"] == "Green"


@pytest.mark.parametrize("raw", ["Green ", "Yellow", "Finish", "Red   ", "      "])
def test_heartbeat_flag_padding_is_stripped(raw):
    """Each observed flag value reaches the caller with its padding gone.

    These are every value the fixtures contain.  ``"Yellow"`` was captured
    during a race that ran green, went yellow for 109s and returned to green —
    the only flag round trip on record.  Blank is ambiguous: pre-session,
    formation lap, between sessions and post-finish all use it, so it must not
    be read as a state of its own.
    """
    msg = parse_line(f'$F,9999,"00:00:00","07:59:59","00:00:00","{raw}"')
    assert msg["flag"] == raw.strip()


def test_captured_flag_fields_are_all_six_characters():
    """The ``$F`` flag field is fixed width, measured over the fixtures.

    Derived rather than asserted against a hard-coded list, so it fails if a
    fixture is ever committed carrying a differently sized field — which is the
    only way this repository would learn the width is installation-specific
    rather than protocol-wide.  Fixed width is what makes a name longer than
    six characters truncate instead of pad; see
    :func:`relay.rmonitor_client._parse_heartbeat`.
    """
    widths: dict[int, int] = {}
    for path in FIXTURE_FILES:
        for line in path.read_text(errors="replace").splitlines():
            m = _F_FLAG_FIELD.search(line)
            if m:
                widths[len(m.group(1))] = widths.get(len(m.group(1)), 0) + 1

    assert sum(widths.values()) > 1000, "fixtures missing: nothing was measured"
    assert set(widths) == {6}, f"non-six-character $F flag fields: {widths}"


# -- $A Competitor ----------------------------------------------------------

def test_competitor_a():
    msg = parse_line('$A,"1234BE","12X",52474,"John","Johnson","USA",5')
    assert msg is not None
    assert msg["type"] == "competitor"
    assert msg["reg_number"] == "1234BE"
    assert msg["number"] == "12X"
    assert msg["transponder"] == "52474"
    assert msg["first_name"] == "John"
    assert msg["last_name"] == "Johnson"
    assert msg["nationality"] == "USA"
    assert msg["class_number"] == "5"


def test_competitor_transponder_is_not_always_numeric():
    """A transponder may be alphanumeric, so it stays a string.

    ``"NE2"`` and ``"NE4"`` are real values from a live feed — club hire units,
    alongside ordinary numeric ones.  Anything coercing this field to ``int``
    raises on them.
    """
    msg = parse_line('$A,"26","26",NE4,"Michael","Barron","Legend Coupe",1')
    assert msg["transponder"] == "NE4"


# -- $COMP Extended competitor ----------------------------------------------

def test_competitor_comp():
    msg = parse_line('$COMP,"21","21",1,"Farnbacher /","James","Panoz Esperante",""')
    assert msg is not None
    assert msg["type"] == "competitor"
    assert msg["reg_number"] == "21"
    assert msg["number"] == "21"
    assert msg["class_number"] == "1"
    assert msg["first_name"] == "Farnbacher /"
    assert msg["last_name"] == "James"
    assert msg["nationality"] == "Panoz Esperante"
    assert msg["additional_data"] == ""


# -- $B Run information -----------------------------------------------------

def test_run():
    msg = parse_line('$B,32,"Test Session 4"')
    assert msg is not None
    assert msg["type"] == "run"
    assert msg["unique_number"] == "32"
    assert msg["description"] == "Test Session 4"


def test_every_opened_session_is_closed_by_a_95_carrying_its_description():
    """``$B,95`` closes the session that ran, tracked as a ``unique_number`` edge.

    Follows the active run the way the protocol rule states it: a *number*
    becoming current opens a session — never a description, or two sessions
    sharing one name would read as a repeat — and a 95 closes it only if it
    repeats that session's description.  The same number arriving again is not
    a boundary, which is the whole point: the four full-session samples send
    their run record between 1 and 264 times and still open one session each.

    Two properties of the feed shape the walk.  A 95 may name a session the
    sample never opened, because a feed joined mid-meeting sees the previous
    session's closing record first; those close nothing rather than erroring.
    And a sample cut off mid-session would leave a run active at EOF — none
    here does, which is why the counts are equal; a truncated capture added to
    the corpus should fail this and be reckoned with rather than silently
    exempted.  See :meth:`server.race_state.RaceState._init`.
    """
    opened: dict[str, int] = {}
    closed: dict[str, int] = {}
    left_open = []
    for path in FIXTURE_FILES:
        active_number = active_description = None
        opened[path.name] = closed[path.name] = 0
        for line in path.read_text(errors="replace").splitlines():
            m = _B_RUN_RECORD.search(line)
            if not m:
                continue
            number, description = m.group(1), m.group(2)
            msg = parse_line(m.group(0))
            assert msg["unique_number"] == number and msg["description"] == description
            if number == "95":
                if active_number is not None and description == active_description:
                    closed[path.name] += 1
                    active_number = active_description = None
            elif number != active_number:
                if active_number is not None:
                    left_open.append((path.name, active_number, active_description))
                active_number, active_description = number, description
                opened[path.name] += 1
        if active_number is not None:
            left_open.append((path.name, active_number, active_description))

    assert not left_open, f"sessions no $B,95 closed by description: {left_open}"
    assert opened == closed, f"opened/closed session counts differ: {opened} vs {closed}"
    # Four of the five samples carry a whole session; the caution excerpt is a
    # mid-race window and carries no $B at all.
    assert sum(closed.values()) == 4, f"expected 4 closures across the corpus: {closed}"


def test_corpus_contains_a_green_yellow_green_round_trip():
    """A caution and its recovery are on record, not just in the notes.

    ``capture_20260912T143259-caution-excerpt.log`` holds the only flag round
    trip this repository has: 109 seconds of ``"Yellow"`` between green either
    side.  Derived here so the observation the pitfall cites is verifiable from
    the repository rather than from a capture that was never committed.
    """
    flags = []
    text = (_REPO_ROOT / "captures" / "capture_20260912T143259-caution-excerpt.log").read_text()
    for line in text.splitlines():
        m = _F_FLAG_FIELD.search(line)
        if m and (not flags or m.group(1) != flags[-1]):
            flags.append(m.group(1))

    assert flags == ["Green ", "Yellow", "Green "], f"not a round trip: {flags}"


def test_captured_run_records_repeat_so_a_boundary_is_an_edge():
    """``$B`` numbers recur, measured over the committed samples.

    The pitfall rests on two facts, derived here rather than asserted from
    memory.  ``95`` appears in the 2009 Sebring reference exports *and* in the
    Orbits capture, so the sentinel is not one installation's habit.  And run
    numbers recur — a live session re-sends its own record up to 264 times in
    one Sebring session, and even ``95`` repeats — so acting on every arrival
    would reopen a session already running; only the change of
    ``unique_number`` is a boundary.

    Note what is deliberately *not* asserted: that every sample closes with a
    ``$B,95``.  A capture stopped mid-session has no closing record for it, so
    the absence of a sentinel says nothing — which is itself why only the
    transition can be acted on.  See :meth:`server.race_state.RaceState._init`.
    """
    per_dir: dict[str, dict[str, int]] = {}
    for path in FIXTURE_FILES:
        counts = per_dir.setdefault(path.parent.name, {})
        for line in path.read_text(errors="replace").splitlines():
            m = _B_RUN_NUMBER.search(line)
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1

    assert per_dir, "fixtures missing: nothing was measured"
    without_sentinel = [d for d, counts in per_dir.items() if "95" not in counts]
    assert not without_sentinel, f"no $B,95 in the samples under: {without_sentinel}"

    all_counts: dict[str, int] = {}
    for counts in per_dir.values():
        for number, count in counts.items():
            all_counts[number] = all_counts.get(number, 0) + count
    assert any(n != "95" and c > 1 for n, c in all_counts.items()), \
        "no active run record repeats; is $B unique after all?"
    assert all_counts["95"] > 1, "the sentinel never repeats; is 95 an edge after all?"


# -- $C Class information ---------------------------------------------------

def test_class_info():
    msg = parse_line('$C,1,"Formula 100"')
    assert msg is not None
    assert msg["type"] == "class_info"
    assert msg["unique_number"] == "1"
    assert msg["description"] == "Formula 100"


# -- $E Setting information -------------------------------------------------

def test_setting_trackname():
    msg = parse_line('$E,"TRACKNAME","Sebring International Raceway"')
    assert msg is not None
    assert msg["type"] == "setting"
    assert msg["description"] == "TRACKNAME"
    assert msg["value"] == "Sebring International Raceway"


def test_setting_tracklength():
    msg = parse_line('$E,"TRACKLENGTH","3.700"')
    assert msg["type"] == "setting"
    assert msg["value"] == "3.700"


# -- $G Race / position information -----------------------------------------

def test_race_info():
    msg = parse_line('$G,1,"21",,"00:00:56.665"')
    assert msg is not None
    assert msg["type"] == "race_info"
    assert msg["position"] == "1"
    assert msg["reg_number"] == "21"
    assert msg["laps"] == ""
    assert msg["total_time"] == "00:00:56.665"


# -- $H Practice / qualifying -----------------------------------------------

def test_qual_info():
    msg = parse_line('$H,1,"21",0,"00:59:59.999"')
    assert msg is not None
    assert msg["type"] == "qual_info"
    assert msg["position"] == "1"
    assert msg["reg_number"] == "21"
    assert msg["best_lap"] == "0"
    assert msg["best_lap_time"] == "00:59:59.999"


# -- $I Init record ---------------------------------------------------------

def test_init():
    msg = parse_line('$I,"10:03:08","27 Jan 09"')
    assert msg is not None
    assert msg["type"] == "init"
    assert msg["time_of_day"] == "10:03:08"
    assert msg["date"] == "27 Jan 09"


# -- $J Passing information -------------------------------------------------

def test_passing():
    msg = parse_line('$J,"21","00:02:06.403","00:04:34.359"')
    assert msg is not None
    assert msg["type"] == "passing"
    assert msg["reg_number"] == "21"
    assert msg["lap_time"] == "00:02:06.403"
    assert msg["total_time"] == "00:04:34.359"


def test_passing_zero():
    msg = parse_line('$J,"21","00:00:00.000","00:00:56.665"')
    assert msg["lap_time"] == "00:00:00.000"


# -- $SP/$SR Lap information (undocumented) ----------------------------------

def test_lap_info_sp():
    msg = parse_line('$SP,1,"21",3,"00:01:45.123"')
    assert msg is not None
    assert msg["type"] == "lap_info"
    assert msg["position"] == "1"
    assert msg["reg_number"] == "21"
    assert msg["lap_number"] == "3"
    assert msg["lap_time"] == "00:01:45.123"


def test_lap_info_sr():
    msg = parse_line('$SR,2,"45",5,"00:02:10.456"')
    assert msg is not None
    assert msg["type"] == "lap_info"


# -- Unknown / malformed lines -----------------------------------------------

def test_unknown_command():
    msg = parse_line('$Z,1,2,3')
    assert msg is None


def test_empty_line():
    msg = parse_line('')
    assert msg is None
