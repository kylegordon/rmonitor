"""Tests for race state management."""

import pytest

from server.race_state import RaceState


@pytest.fixture
def state():
    return RaceState()


def test_heartbeat_updates_flag(state):
    ev = state.process({
        "type": "heartbeat",
        "laps_to_go": "10",
        "time_to_go": "00:10:00",
        "time_of_day": "14:00:00",
        "race_time": "00:50:00",
        "flag": "Green",
    })
    assert ev == "heartbeat"
    assert state.flag == "Green"
    assert state.race_time == "00:50:00"


def test_competitor_registration(state):
    ev = state.process({
        "type": "competitor",
        "reg_number": "21",
        "number": "21",
        "transponder": "12345",
        "first_name": "John",
        "last_name": "Smith",
        "nationality": "USA",
        "class_number": "1",
    })
    assert ev == "competitor"
    assert "21" in state.competitors
    c = state.competitors["21"]
    assert c["first_name"] == "John"
    assert c["last_name"] == "Smith"
    assert c["nationality"] == "USA"


def test_competitor_name_not_blanked_by_empty_update(state):
    state.process({
        "type": "competitor",
        "reg_number": "21",
        "number": "21",
        "first_name": "John",
        "last_name": "Smith",
        "nationality": "USA",
        "class_number": "1",
    })
    state.process({
        "type": "competitor",
        "reg_number": "21",
        "number": "21",
        "first_name": "",
        "last_name": "Doe",
        "nationality": "",
        "class_number": "",
    })
    c = state.competitors["21"]
    assert c["first_name"] == "John"  # Not overwritten by empty
    assert c["last_name"] == "Doe"    # Updated to new value


def test_class_info(state):
    state.process({"type": "class_info", "unique_number": "1", "description": "GT"})
    assert state.classes["1"] == "GT"


def test_setting_track_name(state):
    state.process({"type": "setting", "description": "TRACKNAME", "value": "Sebring"})
    assert state.track_name == "Sebring"


def test_setting_track_length(state):
    state.process({"type": "setting", "description": "TRACKLENGTH", "value": "3.700"})
    assert state.track_length_miles == 3.7


def test_race_info_updates_position(state):
    state.process({
        "type": "race_info",
        "position": "1",
        "reg_number": "21",
        "laps": "5",
        "total_time": "00:10:00.000",
    })
    c = state.competitors["21"]
    assert c["position"] == "1"
    assert c["laps"] == "5"


def test_qual_info_updates_best_lap(state):
    state.process({
        "type": "qual_info",
        "position": "1",
        "reg_number": "21",
        "best_lap": "3",
        "best_lap_time": "00:01:45.123",
    })
    c = state.competitors["21"]
    assert c["best_lap_time"] == "00:01:45.123"


def test_passing_updates_lap_time(state):
    state.process({"type": "setting", "description": "TRACKLENGTH", "value": "3.700"})
    state.process({
        "type": "passing",
        "reg_number": "21",
        "lap_time": "00:02:00.000",
        "total_time": "00:10:00.000",
    })
    c = state.competitors["21"]
    assert c["last_lap_time"] == "00:02:00.000"
    # Speed = 3.7 miles / (120 seconds / 3600) = 3.7 / 0.0333... = 111.0 mph
    assert c["last_lap_speed_mph"] == 111.0


def test_init_clears_state(state):
    state.process({
        "type": "competitor",
        "reg_number": "21",
        "number": "21",
        "first_name": "John",
        "last_name": "Smith",
        "nationality": "",
        "class_number": "",
    })
    assert len(state.competitors) == 1

    ev = state.process({"type": "init", "time_of_day": "10:00:00", "date": "01 Jan 25"})
    assert ev == "init"
    assert len(state.competitors) == 0
    assert state.flag == ""


def test_snapshot_sorted_by_position(state):
    state.process({"type": "race_info", "position": "3", "reg_number": "A", "laps": "", "total_time": ""})
    state.process({"type": "race_info", "position": "1", "reg_number": "B", "laps": "", "total_time": ""})
    state.process({"type": "race_info", "position": "2", "reg_number": "C", "laps": "", "total_time": ""})
    snap = state.snapshot()
    positions = [e["position"] for e in snap["entries"]]
    assert positions == ["1", "2", "3"]


def test_snapshot_sorted_by_best_lap_in_qualifying(state):
    state.process({"type": "qual_info", "position": "3", "reg_number": "A", "best_lap": "1", "best_lap_time": "00:01:50.000"})
    state.process({"type": "qual_info", "position": "1", "reg_number": "B", "best_lap": "2", "best_lap_time": "00:01:40.000"})
    state.process({"type": "qual_info", "position": "2", "reg_number": "C", "best_lap": "1", "best_lap_time": "00:01:45.000"})
    snap = state.snapshot()
    numbers = [e["reg_number"] for e in snap["entries"]]
    assert numbers == ["B", "C", "A"]  # fastest first


def test_snapshot_qualifying_no_lap_time_sorted_last(state):
    state.process({"type": "qual_info", "position": "1", "reg_number": "A", "best_lap": "1", "best_lap_time": "00:01:45.000"})
    state.process({"type": "qual_info", "position": "2", "reg_number": "B", "best_lap": "", "best_lap_time": ""})
    snap = state.snapshot()
    numbers = [e["reg_number"] for e in snap["entries"]]
    assert numbers == ["A", "B"]  # timed entry first, untimed last


def test_competitor_keyed_by_reg_number_not_displayed_number(state):
    """Guards AGENTS.md pitfall 1: the two are different keys and can disagree."""
    state.process({
        "type": "competitor",
        "reg_number": "21",
        "number": "12X",
        "first_name": "John",
        "last_name": "Smith",
        "nationality": "USA",
        "class_number": "1",
    })
    assert "21" in state.competitors
    assert "12X" not in state.competitors
    assert state.competitors["21"]["number"] == "12X"


def test_qual_info_during_a_race_does_not_overwrite_race_positions(state):
    """Guards AGENTS.md pitfall 2: some Orbits setups send $H during a race.

    Best-lap fields must still update, or the guard would cost the feature the
    out-of-session $H exists to provide.
    """
    state.process({"type": "race_info", "position": "1", "reg_number": "A", "laps": "5", "total_time": "00:10:00.000"})
    state.process({"type": "qual_info", "position": "9", "reg_number": "A", "best_lap": "3", "best_lap_time": "00:01:45.000"})
    assert state.is_qualifying is False
    assert state.competitors["A"]["position"] == "1"
    assert state.competitors["A"]["best_lap_time"] == "00:01:45.000"


def test_is_qualifying_cleared_by_race_info(state):
    state.process({"type": "qual_info", "position": "1", "reg_number": "A", "best_lap": "1", "best_lap_time": "00:01:45.000"})
    assert state.is_qualifying is True
    state.process({"type": "race_info", "position": "1", "reg_number": "A", "laps": "5", "total_time": "00:10:00.000"})
    assert state.is_qualifying is False


def test_snapshot_resolves_class_description(state):
    state.process({"type": "class_info", "unique_number": "1", "description": "GT3"})
    state.process({
        "type": "competitor",
        "reg_number": "21",
        "number": "21",
        "first_name": "A",
        "last_name": "B",
        "nationality": "",
        "class_number": "1",
    })
    snap = state.snapshot()
    assert snap["entries"][0]["class_description"] == "GT3"


def test_lap_info_updates_state(state):
    state.process({"type": "setting", "description": "TRACKLENGTH", "value": "2.500"})
    state.process({
        "type": "lap_info",
        "position": "1",
        "reg_number": "42",
        "lap_number": "7",
        "lap_time": "00:01:30.000",
    })
    c = state.competitors["42"]
    assert c["last_lap_time"] == "00:01:30.000"
    assert c["laps"] == "7"
    assert c["position"] == "1"
    # Speed = 2.5 / (90/3600) = 100.0 mph
    assert c["last_lap_speed_mph"] == 100.0


def _set_purple_flag(state):
    state.process({
        "type": "heartbeat",
        "laps_to_go": "9999",
        "time_to_go": "00:00:00",
        "time_of_day": "14:00:00",
        "race_time": "00:00:10",
        "flag": "Purple",
    })


def test_snapshot_purple_flag_sorted_by_total_time(state):
    """Cars with earlier total_time come first under a purple flag."""
    _set_purple_flag(state)
    # Provide race_info so competitors exist; total_time is set via passing
    state.process({"type": "race_info", "position": "3", "reg_number": "A", "laps": "", "total_time": "00:00:08.000"})
    state.process({"type": "race_info", "position": "1", "reg_number": "B", "laps": "", "total_time": "00:00:05.000"})
    state.process({"type": "race_info", "position": "2", "reg_number": "C", "laps": "", "total_time": "00:00:06.000"})
    snap = state.snapshot()
    reg_numbers = [e["reg_number"] for e in snap["entries"]]
    assert reg_numbers == ["B", "C", "A"]  # earliest total_time first


def test_snapshot_purple_flag_no_total_time_sorted_last(state):
    """Cars without a total_time go last under a purple flag."""
    _set_purple_flag(state)
    state.process({"type": "race_info", "position": "1", "reg_number": "A", "laps": "", "total_time": "00:00:05.000"})
    state.process({"type": "race_info", "position": "2", "reg_number": "B", "laps": "", "total_time": ""})
    snap = state.snapshot()
    reg_numbers = [e["reg_number"] for e in snap["entries"]]
    assert reg_numbers == ["A", "B"]  # timed entry first, untimed last


def test_snapshot_purple_flag_zero_total_time_sorted_last(state):
    """Cars with a zero total_time are treated as not-yet-out under a purple flag."""
    _set_purple_flag(state)
    state.process({"type": "race_info", "position": "1", "reg_number": "A", "laps": "", "total_time": "00:00:07.000"})
    state.process({"type": "race_info", "position": "2", "reg_number": "B", "laps": "", "total_time": "00:00:00.000"})
    snap = state.snapshot()
    reg_numbers = [e["reg_number"] for e in snap["entries"]]
    assert reg_numbers == ["A", "B"]  # non-zero time first, zero time last


def test_snapshot_opening_lap_sorted_by_total_time(state):
    """When no competitor has a position yet, sort by order over the line."""
    state.process({"type": "passing", "reg_number": "A", "lap_time": "00:01:00.000", "total_time": "00:05:00.000"})
    state.process({"type": "passing", "reg_number": "B", "lap_time": "00:01:00.000", "total_time": "00:03:00.000"})
    state.process({"type": "passing", "reg_number": "C", "lap_time": "00:01:00.000", "total_time": "00:04:00.000"})
    snap = state.snapshot()
    reg_numbers = [e["reg_number"] for e in snap["entries"]]
    assert reg_numbers == ["B", "C", "A"]  # earliest total_time first


def test_snapshot_opening_lap_not_yet_crossed_sorted_last(state):
    """Cars that haven't crossed the line yet go after those that have."""
    state.process({"type": "passing", "reg_number": "A", "lap_time": "00:01:00.000", "total_time": "00:04:00.000"})
    state.process({"type": "competitor", "reg_number": "B", "number": "7", "first_name": "", "last_name": "", "nationality": "", "class_number": ""})
    snap = state.snapshot()
    reg_numbers = [e["reg_number"] for e in snap["entries"]]
    assert reg_numbers == ["A", "B"]  # crossed-line first, not-yet-out last


def test_snapshot_positions_take_precedence_over_total_time(state):
    """Cars with an assigned position always sort before those with only total_time."""
    state.process({"type": "race_info", "position": "2", "reg_number": "A", "laps": "1", "total_time": "00:05:00.000"})
    state.process({"type": "race_info", "position": "1", "reg_number": "B", "laps": "1", "total_time": "00:03:00.000"})
    # C has crossed the line but has not yet been assigned a position
    state.process({"type": "passing", "reg_number": "C", "lap_time": "00:01:00.000", "total_time": "00:04:00.000"})
    snap = state.snapshot()
    reg_numbers = [e["reg_number"] for e in snap["entries"]]
    assert reg_numbers == ["B", "A", "C"]  # positions first, then by total_time


def test_interleaved_G_H_preserves_race_positions(state):
    """$H messages must not overwrite race positions set by $G during a race.

    In the real protocol, $G (race position) and $H (best-lap ranking) are
    interleaved.  Before this fix, $H could overwrite a car's race position
    with its best-lap ranking AND flip _is_qualifying to True, breaking sort.
    """
    state.process({
        "type": "heartbeat", "laps_to_go": "9999", "time_to_go": "00:00:00",
        "time_of_day": "08:00:55", "race_time": "00:00:57", "flag": "Green",
    })
    # Interleaved $G and $H as seen in real captures
    state.process({"type": "race_info", "position": "1", "reg_number": "21", "laps": "", "total_time": "00:00:56.665"})
    state.process({"type": "qual_info", "position": "1", "reg_number": "21", "best_lap": "0", "best_lap_time": "00:59:59.999"})
    state.process({"type": "race_info", "position": "2", "reg_number": "45", "laps": "", "total_time": "00:59:59.999"})
    state.process({"type": "race_info", "position": "3", "reg_number": "92", "laps": "", "total_time": "00:59:59.999"})
    state.process({"type": "race_info", "position": "4", "reg_number": "44", "laps": "", "total_time": "00:59:59.999"})
    state.process({"type": "race_info", "position": "5", "reg_number": "46", "laps": "", "total_time": "00:59:59.999"})
    state.process({"type": "qual_info", "position": "3", "reg_number": "45", "best_lap": "0", "best_lap_time": "00:59:59.999"})

    # Car 45 should still have race position 2 (from $G), not 3 (from $H)
    assert state.competitors["45"]["position"] == "2"
    assert state.is_qualifying is False

    snap = state.snapshot()
    positions = [e["position"] for e in snap["entries"]]
    assert positions == ["1", "2", "3", "4", "5"]


def test_green_flag_overrides_qualifying_sort(state):
    """Even if _is_qualifying lingers, green flag forces race position sort."""
    # Start with qualifying
    state.process({"type": "qual_info", "position": "1", "reg_number": "A", "best_lap": "1", "best_lap_time": "00:01:50.000"})
    state.process({"type": "qual_info", "position": "2", "reg_number": "B", "best_lap": "2", "best_lap_time": "00:01:40.000"})
    assert state.is_qualifying is True

    # Green flag with race positions (different order from qualifying)
    state.process({
        "type": "heartbeat", "laps_to_go": "10", "time_to_go": "00:10:00",
        "time_of_day": "14:00:00", "race_time": "00:50:00", "flag": "Green",
    })
    state.process({"type": "race_info", "position": "1", "reg_number": "B", "laps": "5", "total_time": "00:10:00.000"})
    state.process({"type": "race_info", "position": "2", "reg_number": "A", "laps": "5", "total_time": "00:10:05.000"})

    snap = state.snapshot()
    reg_numbers = [e["reg_number"] for e in snap["entries"]]
    assert reg_numbers == ["B", "A"]  # race position order, not best-lap order



# ---- session mode derivation ----

@pytest.mark.parametrize("description,expected_mode", [
    # Warm-up variants
    ("Warm up",              "Practice"),
    ("Warm-up",              "Practice"),
    ("Warmup",               "Practice"),
    ("warm up session",      "Practice"),
    ("WARM UP",              "Practice"),
    # Practice variants
    ("Practice 1",           "Practice"),
    ("Free Practice",        "Practice"),
    ("Prac 1",               "Practice"),
    # Familiarisation
    ("Familiarisation",      "Practice"),
    ("familiarisation run",  "Practice"),
])
def test_session_mode_practice_keywords(state, description, expected_mode):
    """Descriptions that should derive 'Practice' mode."""
    state.process({"type": "run", "description": description})
    assert state.snapshot()["session_mode"] == expected_mode


def test_session_mode_race(state):
    """A race session (with $G data) derives 'Race'."""
    state.process({"type": "run", "description": "Race 1"})
    state.process({
        "type": "race_info", "position": "1", "reg_number": "1",
        "laps": "1", "total_time": "00:01:30.000",
    })
    assert state.snapshot()["session_mode"] == "Race"


def test_session_mode_qualifying_from_flag(state):
    """$H messages (without prior $G) set qualifying mode."""
    state.process({
        "type": "qual_info", "position": "1", "reg_number": "1",
        "best_lap": "1", "best_lap_time": "00:01:50.000",
    })
    assert state.snapshot()["session_mode"] == "Qualifying"


def test_session_mode_qualifying_from_description(state):
    """'Qual' in description derives 'Qualifying' even without $H."""
    state.process({"type": "run", "description": "Qualifying 1"})
    assert state.snapshot()["session_mode"] == "Qualifying"


def test_session_mode_warm_up_with_race_info_is_practice(state):
    """Warm-up description takes priority over _seen_race_info flag."""
    state.process({"type": "run", "description": "Warm-up"})
    state.process({
        "type": "race_info", "position": "1", "reg_number": "1",
        "laps": "1", "total_time": "00:01:30.000",
    })
    assert state.snapshot()["session_mode"] == "Practice"


# ---------------------------------------------------------------------------
# Regression tests: /api/ingest carries untrusted JSON from the network.
# A non-string value in a field normally treated as a string must not crash
# snapshot() (which is also called from the unsupervised periodic broadcast
# loop, where an uncaught exception would silently kill live updates for
# every connected client).
# ---------------------------------------------------------------------------

def test_snapshot_survives_non_string_flag(state):
    state.process({
        "type": "heartbeat", "laps_to_go": "5", "time_to_go": "00:05:00",
        "time_of_day": "14:00:00", "race_time": "00:50:00", "flag": 123,
    })
    snap = state.snapshot()
    assert snap["flag"] == "123"


def test_snapshot_survives_non_string_total_time(state):
    state.process({
        "type": "race_info", "position": "1", "reg_number": "1",
        "laps": 3, "total_time": 12345,
    })
    snap = state.snapshot()
    entry = snap["entries"][0]
    assert entry["total_time"] == "12345"
    assert entry["laps"] == "3"


def test_snapshot_survives_non_string_best_lap_time(state):
    state.process({
        "type": "qual_info", "position": "1", "reg_number": "1",
        "best_lap": 2, "best_lap_time": 61.5,
    })
    snap = state.snapshot()
    assert snap["entries"][0]["best_lap_time"] == "61.5"


def test_lap_time_seconds_ignores_non_string_input():
    from server.race_state import _lap_time_seconds
    assert _lap_time_seconds(12345) is None
    assert _lap_time_seconds(["not", "a", "string"]) is None


def _feed_capture_sequence(state):
    """Feed the three-car ``$G`` sequence taken from a real capture.

    Verbatim from ``captures/capture_20260517T134241.log``: leader 15 completes
    lap 13 at 873.950 s, cars 64 and 88 follow it round on lap 13, then 15
    completes lap 14.  That leaves the leader one lap ahead of both, which is
    the ordinary mid-race state and the one a naive gap gets wrong.
    """
    state.process({
        "type": "race_info", "position": "1", "reg_number": "15",
        "laps": "13", "total_time": "00:14:33.950",
    })
    state.process({
        "type": "race_info", "position": "2", "reg_number": "64",
        "laps": "13", "total_time": "00:15:27.056",
    })
    state.process({
        "type": "race_info", "position": "3", "reg_number": "88",
        "laps": "13", "total_time": "00:15:48.743",
    })
    state.process({
        "type": "race_info", "position": "1", "reg_number": "15",
        "laps": "14", "total_time": "00:15:38.661",
    })


def _feed_lapped_field(state):
    """Feed a real moment in which two cars are two laps down.

    Also verbatim from ``captures/capture_20260517T134241.log``, in the order
    the feed sent it: 101 leads on lap 6, 15 takes the lead on lap 8 with 64
    and 88 on the same lap, and 101 and 1 reappear at P4 and P5 still on lap 6.
    The index therefore holds a genuine leader time for both laps — 352.784 at
    lap 6 (101's own, while it led) and 554.151 at lap 8 (15's).
    """
    state.process({
        "type": "race_info", "position": "1", "reg_number": "101",
        "laps": "6", "total_time": "00:05:52.784",
    })
    state.process({
        "type": "race_info", "position": "1", "reg_number": "15",
        "laps": "8", "total_time": "00:09:14.151",
    })
    state.process({
        "type": "race_info", "position": "2", "reg_number": "64",
        "laps": "8", "total_time": "00:09:32.950",
    })
    state.process({
        "type": "race_info", "position": "3", "reg_number": "88",
        "laps": "8", "total_time": "00:10:00.914",
    })
    state.process({
        "type": "race_info", "position": "4", "reg_number": "101",
        "laps": "6", "total_time": "00:05:52.784",
    })
    state.process({
        "type": "race_info", "position": "5", "reg_number": "1",
        "laps": "6", "total_time": "00:07:47.102",
    })


def _by_reg(snap):
    return {e["reg_number"]: e for e in snap["entries"]}


def test_gap_and_diff_from_capture_sequence(state):
    """Gap and Diff on the worked example from the capture."""
    _feed_capture_sequence(state)
    entries = _by_reg(state.snapshot())
    # Leader index: lap 13 = 873.950 (car 15), lap 14 = 938.661 (car 15).
    # 64: 927.056 - 873.950 = 53.106   88: 948.743 - 873.950 = 74.793
    # Gap 88 behind 64: 74.793 - 53.106 = 21.687, and both are on lap 13, so
    # the direct check agrees: 948.743 - 927.056 = 21.687.
    assert entries["15"]["diff_leader_seconds"] is None
    assert entries["15"]["gap_ahead_seconds"] is None
    assert entries["15"]["diff_leader_laps"] is None
    assert entries["15"]["gap_ahead_laps"] is None
    assert entries["64"]["diff_leader_seconds"] == pytest.approx(53.106, abs=1e-3)
    assert entries["64"]["gap_ahead_seconds"] == pytest.approx(53.106, abs=1e-3)
    assert entries["88"]["diff_leader_seconds"] == pytest.approx(74.793, abs=1e-3)
    assert entries["88"]["gap_ahead_seconds"] == pytest.approx(21.687, abs=1e-3)


def test_diff_equals_running_sum_of_gaps(state):
    """Each Diff equals the running sum of the Gap values above it.

    The strongest single assertion available on this derivation: it fails on an
    error in either column, because the two are computed from the same
    intermediate values and must stay consistent with each other.
    """
    _feed_capture_sequence(state)
    running = 0.0
    for entry in state.snapshot()["entries"][1:]:
        running += entry["gap_ahead_seconds"]
        assert entry["diff_leader_seconds"] == pytest.approx(running, abs=1e-3)
    # 53.106 + 21.687 = 74.793
    assert running == pytest.approx(74.793, abs=1e-3)


def test_leader_row_has_no_gap_or_diff(state):
    """The leader's own row carries no interval, so the page renders an em-dash.

    Emitted as *None* rather than special-cased in the template: ``0.000``
    against yourself is a number that reads as a real measurement.
    """
    _feed_capture_sequence(state)
    leader = state.snapshot()["entries"][0]
    assert leader["reg_number"] == "15"
    for field in (
        "gap_ahead_seconds", "gap_ahead_laps",
        "diff_leader_seconds", "diff_leader_laps",
    ):
        assert leader[field] is None


def test_gap_is_positive_when_the_leader_is_a_lap_ahead(state):
    """A car a lap down is reported as behind the leader, never ahead of it.

    The regression guard for the sign error that sinks the obvious
    implementation.  ``captures/capture_20260517T134241.log`` holds

        $G,1,"15",13,"00:14:33.950"
        $G,2,"64",13,"00:15:27.056"
        $G,1,"15",14,"00:15:38.661"

    After the third line the snapshot has 15 at lap 14 / 938.661 and 64 at lap
    13 / 927.056.  Subtracting those two ``total_time`` values in one snapshot
    gives 927.056 - 938.661 = -11.605, putting P2 11.6 s *ahead* of the leader,
    with the sign flipping every time the leader crosses the line.  Measured
    against the leader's time at car 64's own lap it is +53.106 s, the true gap.
    """
    _feed_capture_sequence(state)
    car_64 = _by_reg(state.snapshot())["64"]
    assert car_64["diff_leader_seconds"] == pytest.approx(53.106, abs=1e-3)
    assert car_64["diff_leader_seconds"] > 0


def test_two_lap_deficit_reported_as_laps_not_seconds(state):
    """At two laps down or more, Diff reports laps and no time."""
    _feed_lapped_field(state)
    car_101 = _by_reg(state.snapshot())["101"]
    # Leader 15 on lap 8, car 101 on lap 6.
    assert car_101["diff_leader_laps"] == 2
    assert car_101["diff_leader_seconds"] is None


def test_one_lap_deficit_still_reports_a_time(state):
    """At one lap down, Diff is still a time — the threshold is 2, not 1.

    A one-lap difference is the *normal* state: the car ahead has crossed the
    line for this lap and you have not yet.  A threshold of 1 would flicker
    between ``+1 L`` and a time roughly once per lap, which is why the
    same-lap-only alternative was rejected.
    """
    _feed_capture_sequence(state)
    car_64 = _by_reg(state.snapshot())["64"]
    # Leader 15 on lap 14, car 64 on lap 13 — a deficit of exactly one.
    assert car_64["diff_leader_laps"] is None
    assert isinstance(car_64["diff_leader_seconds"], float)


def test_gap_lap_deficit_is_measured_against_the_car_ahead(state):
    """Each column uses its own reference, so the two can disagree.

    Car 1 is two laps behind the leader but on the same lap as car 101 directly
    ahead of it, so Diff reports laps while Gap reports a time.
    """
    _feed_lapped_field(state)
    car_1 = _by_reg(state.snapshot())["1"]
    assert car_1["diff_leader_laps"] == 2
    assert car_1["gap_ahead_laps"] is None
    # Both on lap 6, whose leader time is 352.784: 467.102 - 352.784 = 114.318
    assert isinstance(car_1["gap_ahead_seconds"], float)
    assert car_1["gap_ahead_seconds"] == pytest.approx(114.318, abs=1e-3)


def test_qualifying_gap_and_diff_from_best_laps(state):
    """In qualifying both columns derive from best lap times.

    ``$H`` carries no cumulative time, so there is nothing to measure against
    the leader index; the reference is the fastest lap of the session instead.
    No lap deficit applies in qualifying.
    """
    for pos, reg, best in (
        ("1", "15", "00:01:40.000"),
        ("2", "64", "00:01:41.500"),
        ("3", "88", "00:01:43.000"),
    ):
        state.process({
            "type": "qual_info", "position": pos, "reg_number": reg,
            "best_lap": "5", "best_lap_time": best,
        })
    assert state.is_qualifying
    entries = state.snapshot()["entries"]
    assert [e["reg_number"] for e in entries] == ["15", "64", "88"]
    for field in (
        "gap_ahead_seconds", "gap_ahead_laps",
        "diff_leader_seconds", "diff_leader_laps",
    ):
        assert entries[0][field] is None
    # 101.5 - 100.0 = 1.5 ; 103.0 - 100.0 = 3.0 ; 103.0 - 101.5 = 1.5
    assert entries[1]["diff_leader_seconds"] == pytest.approx(1.5, abs=1e-3)
    assert entries[1]["gap_ahead_seconds"] == pytest.approx(1.5, abs=1e-3)
    assert entries[2]["diff_leader_seconds"] == pytest.approx(3.0, abs=1e-3)
    assert entries[2]["gap_ahead_seconds"] == pytest.approx(1.5, abs=1e-3)
    for entry in entries:
        assert entry["gap_ahead_laps"] is None
        assert entry["diff_leader_laps"] is None


def test_purple_flag_suppresses_gap_and_diff(state):
    """Both columns go blank under a purple flag.

    A purple flag makes ``snapshot`` sort by ``total_time`` whatever the
    session mode says, so the row above is the car that crossed the timing loop
    just before — not the car ahead on track.  Purple marks formation and pace
    laps, where an interval carries no meaning.
    """
    _set_purple_flag(state)
    _feed_capture_sequence(state)
    entries = state.snapshot()["entries"]
    assert len(entries) == 3
    for entry in entries:
        for field in (
            "gap_ahead_seconds", "gap_ahead_laps",
            "diff_leader_seconds", "diff_leader_laps",
        ):
            assert entry[field] is None


def test_unusable_times_yield_no_gap_or_diff(state):
    """Unusable feed values yield no interval, and never enter the index.

    Each guard is grounded in real feed data.  In
    ``captures/capture_20260517T134241.log`` 14 ``$G`` lines carry a zero
    ``total_time``, and in
    ``examples/2009 Sebring Test ALMS Session 4 - 0800-1000.txt`` 69 ``$G``
    lines carry an empty laps field and 37 carry the ``00:59:59.999``
    "no time set" sentinel, which read as elapsed seconds would put a car an
    hour behind the leader.
    """
    _feed_capture_sequence(state)
    for pos, reg, laps, total in (
        ("4", "zero", "13", "00:00:00.000"),
        ("5", "nolaps", "", "00:16:00.000"),
        ("6", "sentinel", "13", "00:59:59.999"),
    ):
        state.process({
            "type": "race_info", "position": pos, "reg_number": reg,
            "laps": laps, "total_time": total,
        })
    entries = _by_reg(state.snapshot())
    for reg in ("zero", "nolaps", "sentinel"):
        for field in (
            "gap_ahead_seconds", "gap_ahead_laps",
            "diff_leader_seconds", "diff_leader_laps",
        ):
            assert entries[reg][field] is None, f"{reg}.{field}"
    assert state.leader_time_at_lap == pytest.approx(
        {13: 873.950, 14: 938.661}, abs=1e-3
    )


def test_leader_time_at_lap_is_idempotent_across_refresh_bursts(state):
    """Repeating a $G leaves the index unchanged, and a slower car cannot raise it.

    Refresh bursts repeat an identical ``(position, reg, lap, time)`` triple —
    ``$G,1,"15",8,"00:09:14.151"`` arrives three times in
    ``captures/capture_20260517T134241.log`` — so the index keeps the minimum
    rather than the latest value.
    """
    for _ in range(3):
        state.process({
            "type": "race_info", "position": "1", "reg_number": "15",
            "laps": "8", "total_time": "00:09:14.151",
        })
    state.process({
        "type": "race_info", "position": "2", "reg_number": "64",
        "laps": "8", "total_time": "00:09:32.950",
    })
    assert state.leader_time_at_lap == pytest.approx({8: 554.151}, abs=1e-3)


def test_leader_time_at_lap_survives_a_json_round_trip(state):
    """The restored index is keyed by int, so lookups still hit after a restart.

    ``JsonFileStateStore`` persists through ``json``, whose object keys are
    always strings.  Without the coercion in ``_load_dict`` a restored
    ``{"13": 873.95}`` misses every lookup and both columns go silently blank
    after a restart until the feed re-populates the index.
    """
    import json

    _feed_capture_sequence(state)
    restored = RaceState()
    restored._load_dict(json.loads(json.dumps(state._to_dict())))
    assert all(isinstance(k, int) for k in restored.leader_time_at_lap)
    assert restored.leader_time_at_lap == pytest.approx(
        {13: 873.950, 14: 938.661}, abs=1e-3
    )
    entries = _by_reg(restored.snapshot())
    assert entries["64"]["diff_leader_seconds"] == pytest.approx(53.106, abs=1e-3)
    assert entries["88"]["gap_ahead_seconds"] == pytest.approx(21.687, abs=1e-3)
    # A hand-edited or truncated store degrades to an empty index.
    malformed = RaceState()
    malformed._load_dict({"leader_time_at_lap": "nonsense"})
    assert malformed.leader_time_at_lap == {}


def test_init_clears_leader_time_at_lap(state):
    """A new session clears the index, so gaps are never measured across races."""
    _feed_capture_sequence(state)
    assert state.leader_time_at_lap
    state.process({"type": "init"})
    assert state.leader_time_at_lap == {}
