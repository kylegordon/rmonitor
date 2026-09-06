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
