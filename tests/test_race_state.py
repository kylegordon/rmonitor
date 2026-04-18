"""Tests for race state management."""

import pytest

from app.race_state import RaceState


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
