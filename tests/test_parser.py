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
# Every committed feed sample: the reference Sebring exports and the one capture
# force-added past the `captures/*.log` ignore rule.
FIXTURE_FILES = sorted((_REPO_ROOT / "examples").glob("*.txt")) + sorted(
    (_REPO_ROOT / "captures").glob("*.log")
)
# $F,laps_to_go,"time_to_go","time_of_day","race_time","flag" — the flag field
# raw, before :func:`_tokenize` strips it.
_F_FLAG_FIELD = re.compile(r'\$F,[^,]*,"[^"]*","[^"]*","[^"]*","([^"]*)"')


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


def test_run_95_is_the_session_end_sentinel():
    """``$B,95`` closes a session, carrying the outgoing description.

    Every session across this repository's captures ends this way — a session
    opening as ``$B,27,"Race 5 - 1st Race"`` closes as
    ``$B,95,"Race 5 - 1st Race"``, same description, number 95 — and a feed
    joined between sessions opens with one.  Real run numbers (26, 27, 31-35,
    81) vary per session and can repeat within one, so 95 is the only stable
    boundary signal.  The sentinel must stay distinguishable from the
    description, which is identical on both edges.
    """
    start = parse_line('$B,27,"Race 5 - 1st Race"')
    end = parse_line('$B,95,"Race 5 - 1st Race"')
    assert start["description"] == end["description"]
    assert start["unique_number"] == "27"
    assert end["unique_number"] == "95"


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
