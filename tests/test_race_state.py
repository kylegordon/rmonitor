"""Tests for race state management."""

import time

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


def test_repeated_init_then_repopulate_leaves_state_correct(state):
    """Three consecutive ``$I`` records still end with the session populated.

    A live session start sent ``$I`` three times inside two milliseconds, then
    a duplicated ``$B`` and the usual competitor dump.  Replays that order and
    pins the resulting :class:`RaceState` only: repeated wipes and a duplicated
    ``$B`` leave the session whole, rather than a half-applied batch.  The
    broadcast-per-init half of the invariant is not visible from here, because
    this calls :meth:`RaceState.process` directly; it is pinned over the real
    ingest path by
    ``tests/test_server.py::test_repeated_init_wipes_reach_clients_before_repopulation``.

    The trailing repeat matters on its own: a non-95 ``$B`` is re-sent
    periodically *during* a live session — ``$B,27,"Race 5 - 1st Race"`` arrives
    five times across that race — so an active run record must never be taken
    for a boundary and must leave the field it lands on untouched.
    """
    for _ in range(3):
        assert state.process({"type": "init", "time_of_day": "15:29:14", "date": "12 Sep 26"}) == "init"
    for _ in range(2):
        state.process({"type": "run", "unique_number": "33", "description": "Race 6 - Final 12a"})
    state.process({
        "type": "competitor",
        "reg_number": "79",
        "number": "79",
        "first_name": "Paul",
        "last_name": "Brydon",
        "nationality": "Solution F BMW M3",
        "class_number": "1",
    })

    assert state.run_description == "Race 6 - Final 12a"
    assert len(state.competitors) == 1
    assert state.competitors["79"]["last_name"] == "Brydon"

    # The same run record re-sent mid-session, as the live feed does: it is not
    # a boundary, so the populated field must survive it intact.
    state.process({"type": "run", "unique_number": "33", "description": "Race 6 - Final 12a"})

    assert state.run_description == "Race 6 - Final 12a"
    assert state.competitors["79"]["last_name"] == "Brydon"


def test_init_arriving_after_competitors_wipes_them(state):
    """An ``$I`` mid-stream clears competitors that arrived before it.

    ``$I`` is emitted inconsistently — none on a scoreboard reset, one after a
    finished race, three at a session start — so it cannot be treated as a
    session marker.  It is a live wipe wherever it lands, which is the whole
    reason ordering within the batch matters.
    """
    state.process({
        "type": "competitor",
        "reg_number": "9",
        "number": "9",
        "first_name": "Ron",
        "last_name": "Cumming",
        "nationality": "Nemesis",
        "class_number": "1",
    })
    state.process({"type": "run", "unique_number": "27", "description": "Race 5 - 1st Race"})
    assert len(state.competitors) == 1

    state.process({"type": "init", "time_of_day": "15:19:53", "date": "12 Sep 26"})
    assert state.competitors == {}
    assert state.run_description == ""


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
    """Guards the ``reg_number`` trap (``rmonitor-feed`` skill): the two are different
    keys and can disagree."""
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
    """Guards the session-mode trap (``rmonitor-feed`` skill): some Orbits setups send
    $H during a race.

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
    ("Familiarization",      "Practice"),
    # Other non-competitive session names
    ("Test Session 4",       "Practice"),
    ("Shakedown",            "Practice"),
    ("Sighting laps",        "Practice"),
    ("Untimed session",      "Practice"),
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


def test_early_qual_info_overrides_a_race_description_until_the_first_race_info(state):
    """`_is_qualifying` overrides `$B`; it does not merely fill in for a missing one.

    `_derive_session_mode` tests the flag first and unconditionally, so a feed
    that has already sent `$B,"Race 1"` reports Qualifying — and since the sort
    follows the label, best-lap order — from the first `$H` until the first
    `$G`. Every capture in this repository opens with `$H`, so this window is
    real in every session; it is short only because any `$G` clears the flag
    permanently.
    """
    state.process({"type": "run", "description": "Race 1"})
    state.process({
        "type": "qual_info", "position": "1", "reg_number": "1",
        "best_lap": "1", "best_lap_time": "00:01:50.000",
    })
    snap = state.snapshot()
    assert state.run_description == "Race 1"
    assert snap["session_mode"] == "Qualifying"
    assert snap["sort_mode"] == "best_lap"

    state.process({
        "type": "race_info", "position": "1", "reg_number": "1",
        "laps": "1", "total_time": "00:01:30.000",
    })
    snap = state.snapshot()
    assert snap["session_mode"] == "Race"
    assert snap["sort_mode"] == "position"


def test_bare_test_keyword_also_matches_contest(state):
    """The ``test`` keyword is a bare substring, and this is the cost.

    It also matches ``Contest``, ``Protest`` and ``Fastest``, so a session named
    for any of those reads as practice — and the fallthrough is silent, so it
    would do so invisibly.  The behaviour is pinned here rather than left to be
    "fixed" later by someone who reads it as a bug: no ``$B`` description in
    this repository's ``captures/*.log`` or ``examples/*.txt`` collides, and
    word-boundary matching was not adopted because the corpus does not justify
    the complexity.  Revisit if a real description ever collides.
    """
    state.process({"type": "run", "description": "Contest 1"})
    assert state.snapshot()["session_mode"] == "Practice"


def test_session_mode_warm_up_with_race_info_is_practice(state):
    """Warm-up description takes priority over _seen_race_info flag.

    Feeding a ``$B`` *and* a ``$G`` is exactly the shape that used to label a
    session Practice while running race maths underneath, so the sort mode is
    asserted alongside the label.
    """
    state.process({"type": "run", "description": "Warm-up"})
    state.process({
        "type": "race_info", "position": "1", "reg_number": "1",
        "laps": "1", "total_time": "00:01:30.000",
    })
    snap = state.snapshot()
    assert snap["session_mode"] == "Practice"
    assert snap["sort_mode"] == "best_lap"


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


def _feed_three_cars_on_lap_13(state):
    """Feed the settled three-car state, all on lap 13.

    Verbatim from ``captures/capture_20260517T134241.log``: leader 15 completes
    lap 13 at 873.950 s and cars 64 and 88 follow it round on the same lap.
    Separate from :func:`_feed_capture_sequence` because the interval
    reproductions in this module start here, with every car's stamped pair on
    lap 13 and no ``$G`` in flight.
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


def _feed_capture_sequence(state):
    """Feed the three-car ``$G`` sequence taken from a real capture.

    Verbatim from ``captures/capture_20260517T134241.log``: leader 15 completes
    lap 13 at 873.950 s, cars 64 and 88 follow it round on lap 13, then 15
    completes lap 14.  That leaves the leader one lap ahead of both, which is
    the ordinary mid-race state and the one a naive gap gets wrong.
    """
    _feed_three_cars_on_lap_13(state)
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


def _feed_stalled_car(state):
    """Feed the moment a stalled car drove the live page's negative ``Gap``.

    The defect this reproduces was found on ``timing.glasgownet.com`` during
    Knockhill's "Familiarisation - Q1", whose ``Gap`` column rendered negative
    times (P16 ``-38.448``, P20 ``-1:27.302``) — a car cannot be a negative
    interval behind the one ahead of it.  Replaying
    ``captures/capture_20260419T075703.log`` reproduced four such rows, and
    every one of them is a car that has pitted or retired.

    ``_time_behind_leader`` only advances when a car crosses the timing line,
    which is intended: it answers "how far behind was this car at its own last
    crossing".  A stalled car therefore keeps a small, *frozen* deficit while
    the leader keeps lapping, and the field sorts by the feed's position, which
    puts the stalled car below rows whose larger deficit is current.
    Subtracting the two then gives a negative number.

    This is that arithmetic, with the capture's own leader index — 479.672 at
    lap 8 and 538.553 at lap 9.  Car 17 is circulating, one lap further on and
    genuinely 102.881 s down; car 6 has stopped, frozen at 35.881 s down on the
    lap before.  The feed still lists 17 ahead of 6, so ``Gap`` on car 6 was
    ``35.881 - 102.881 = -67.000`` — which is what the page drew.
    """
    state.process({
        "type": "race_info", "position": "1", "reg_number": "85",
        "laps": "8", "total_time": "00:07:59.672",
    })
    state.process({
        "type": "race_info", "position": "1", "reg_number": "85",
        "laps": "9", "total_time": "00:08:58.553",
    })
    state.process({
        "type": "race_info", "position": "2", "reg_number": "17",
        "laps": "9", "total_time": "00:10:41.434",
    })
    state.process({
        "type": "race_info", "position": "3", "reg_number": "6",
        "laps": "8", "total_time": "00:08:35.553",
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

    The threshold is untouched by the negative-gap fix, which fires on the
    *sign* rather than on the lap difference: car 64 is one lap down with a
    positive interval, so it never reaches that branch and still reports a time.
    """
    _feed_capture_sequence(state)
    car_64 = _by_reg(state.snapshot())["64"]
    # Leader 15 on lap 14, car 64 on lap 13 — a deficit of exactly one.
    assert car_64["diff_leader_laps"] is None
    assert isinstance(car_64["diff_leader_seconds"], float)
    assert car_64["gap_ahead_seconds"] > 0


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


def test_negative_gap_is_blanked(state):
    """A negative Gap is suppressed rather than rendered or converted.

    See :func:`_feed_stalled_car` for how the negative arises.  The live page
    rendered it as a negative time (``-67.000`` for this data), which is the
    defect.  It is blanked rather than turned into ``+1 L`` because the sign
    alone does not identify a stalled car — see
    :func:`test_negative_gap_in_the_crossing_window_is_not_a_lap_deficit`.
    """
    _feed_stalled_car(state)
    car_6 = _by_reg(state.snapshot())["6"]
    # 35.881 (frozen, lap 8) - 102.881 (live, lap 9) = -67.000.
    assert car_6["gap_ahead_seconds"] is None
    assert car_6["gap_ahead_laps"] is None


def test_negative_gap_in_the_crossing_window_is_not_a_lap_deficit(state):
    """Two cars seconds apart must not be reported a lap apart.

    The reason a negative ``Gap`` cannot be converted to ``+1 L``.  The
    subtraction is ``G - d``, where ``G`` is the real on-track gap between the
    two cars and ``d`` is what the car ahead lost to the leader on its latest
    lap, so *any* pair goes negative while the car ahead has crossed for a lap
    the car behind has not and ``d > G``.  Nothing distinguishes that from a
    stalled car at the point of the guard.

    Here car 91 is 20.000 s down at lap 8 and 27.500 s down at lap 9 (it lost
    7.500 s to the leader), and car 55 is 22.000 s down at lap 8 and has not yet
    crossed for lap 9.  The two are 2.000 s apart on the road, but the gap
    computes as ``22.000 - 27.500 = -5.500``.

    Emitting ``+1 L`` here is worse than the negative time it replaced: it is
    plausible and wrong, and it reverts to a time the moment car 55 crosses.
    Replaying ``captures/capture_20260418T132655.log`` that way flipped the
    column between a time and ``+1 L`` 73 times across 12 cars — one car 13
    times, about once a lap — which is precisely the flicker the lap-deficit
    threshold of 2 exists to prevent.
    """
    state.process({
        "type": "race_info", "position": "1", "reg_number": "15",
        "laps": "8", "total_time": "00:07:40.000",
    })
    state.process({
        "type": "race_info", "position": "2", "reg_number": "91",
        "laps": "8", "total_time": "00:08:00.000",
    })
    state.process({
        "type": "race_info", "position": "3", "reg_number": "55",
        "laps": "8", "total_time": "00:08:02.000",
    })
    state.process({
        "type": "race_info", "position": "1", "reg_number": "15",
        "laps": "9", "total_time": "00:08:37.500",
    })
    state.process({
        "type": "race_info", "position": "2", "reg_number": "91",
        "laps": "9", "total_time": "00:09:05.000",
    })
    car_55 = _by_reg(state.snapshot())["55"]
    assert car_55["gap_ahead_laps"] is None, "55 is 2 s behind 91, not a lap"
    assert car_55["gap_ahead_seconds"] is None
    # Diff is unaffected: it normalises against the leader, not the row above.
    assert car_55["diff_leader_seconds"] == pytest.approx(22.0, abs=1e-3)


def test_negative_gap_at_equal_laps_is_blank(state):
    """With no lap difference to report, a negative Gap emits nothing.

    The fallback for the case :meth:`RaceState._apply_intervals` already
    comments on — "early in a session the feed's positions can briefly disagree
    with the on-road order".  Both cars here are on lap 8, so the deficits
    disagree with the feed's order with no lap difference behind it: there is
    nothing true to say and both gap fields stay *None* for an em-dash.

    ``Diff`` is deliberately untouched.  It normalises against the first-placed
    entry, whose own deficit is ~0, so the stalled-car sign error does not reach
    it — no negative ``Diff`` was measured across the seven captures.
    """
    state.process({
        "type": "race_info", "position": "1", "reg_number": "85",
        "laps": "8", "total_time": "00:07:59.672",
    })
    state.process({
        "type": "race_info", "position": "2", "reg_number": "17",
        "laps": "8", "total_time": "00:10:15.553",
    })
    state.process({
        "type": "race_info", "position": "3", "reg_number": "6",
        "laps": "8", "total_time": "00:08:35.553",
    })
    car_6 = _by_reg(state.snapshot())["6"]
    # 35.881 - 135.881 = -100.000, and both cars are on lap 8.
    assert car_6["gap_ahead_seconds"] is None
    assert car_6["gap_ahead_laps"] is None
    assert car_6["diff_leader_seconds"] == pytest.approx(35.881, abs=1e-3)
    assert car_6["diff_leader_laps"] is None


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


def test_practice_session_sorts_by_best_lap_and_derives_intervals_from_them(state):
    """A session the badge calls Practice must use practice maths too.

    ``captures/capture_20260419T075703.log`` opens with

        $B,33,"Familiarisation"

    and then sends 627 ``$G`` lines.  Any ``$G`` clears ``_is_qualifying``
    permanently, so the flag is False for the whole session — as it measures
    False in *every* capture and example file in this repository, including two
    named ``Qualifying``.  Keying the sort and the interval reference on that
    flag therefore ran race maths under a ``PRACTICE`` badge, which is what the
    live page was doing.

    Both must move together.  ``Gap`` means "interval to the row above", so
    best-lap values under the feed's position sort measured *17* negative gaps
    in this capture against 4 for the status quo; under the best-lap sort, none.
    Car 77 holds the session's outright best lap from well down the feed's
    order, so the reordering here is the real one.
    """
    state.process({"type": "run", "description": "Familiarisation"})
    for reg, best in (
        ("77", "00:00:57.502"),
        ("94", "00:00:57.933"),
        ("1", "00:00:58.078"),
        ("97", "00:00:58.277"),
    ):
        state.process({
            "type": "qual_info", "position": "", "reg_number": reg,
            "best_lap": "6", "best_lap_time": best,
        })
    # The feed's own order, which disagrees with the best-lap order.
    for pos, reg, total in (
        ("1", "1", "00:06:10.000"),
        ("2", "97", "00:06:11.000"),
        ("3", "77", "00:06:12.000"),
        ("4", "94", "00:06:13.000"),
    ):
        state.process({
            "type": "race_info", "position": pos, "reg_number": reg,
            "laps": "6", "total_time": total,
        })
    assert state.is_qualifying is False
    snap = state.snapshot()
    assert snap["session_mode"] == "Practice"
    assert snap["sort_mode"] == "best_lap"
    entries = snap["entries"]
    assert [e["reg_number"] for e in entries] == ["77", "94", "1", "97"]
    assert entries[0]["gap_ahead_seconds"] is None
    assert entries[1]["gap_ahead_seconds"] == pytest.approx(0.431, abs=1e-3)
    assert entries[2]["gap_ahead_seconds"] == pytest.approx(0.145, abs=1e-3)
    assert entries[3]["gap_ahead_seconds"] == pytest.approx(0.199, abs=1e-3)
    for entry in entries:
        assert entry["gap_ahead_seconds"] is None or entry["gap_ahead_seconds"] > 0
        assert entry["gap_ahead_laps"] is None
        assert entry["diff_leader_laps"] is None


@pytest.mark.parametrize("description,purple,expected_sort_mode", [
    ("Race 3 - 1st Race", False, "position"),
    ("Familiarisation",   False, "best_lap"),
    ("Qualifying 3",      False, "best_lap"),
    ("Race 3 - 1st Race", True,  "total_time"),
    ("Familiarisation",   True,  "total_time"),
])
def test_snapshot_reports_the_sort_mode_it_used(
    state, description, purple, expected_sort_mode
):
    """The payload states which order it sorted in, rather than implying it.

    ``index.html`` has to know whether the rows are in the feed's order before
    it can decide what ``POS`` means, and the only way to keep the server's
    branch condition from existing in two places is for the server to answer
    the question.  This is the half of that a test can reach: nothing in this
    repository renders the template.
    """
    if purple:
        _set_purple_flag(state)
    state.process({"type": "run", "description": description})
    state.process({
        "type": "race_info", "position": "1", "reg_number": "1",
        "laps": "1", "total_time": "00:01:30.000",
    })
    assert state.snapshot()["sort_mode"] == expected_sort_mode


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


_INTERVAL_FIELDS = (
    "gap_ahead_seconds",
    "gap_ahead_laps",
    "diff_leader_seconds",
    "diff_leader_laps",
)


def _intervals(snap):
    """Copy every entry's four interval fields out of *snap*.

    ``snapshot()`` returns the live competitor dicts rather than copies, so a
    value held across a later ``process()`` call silently compares against
    itself.  Every test below that snapshots more than once compares through
    this helper.
    """
    return {
        e["reg_number"]: {name: e[name] for name in _INTERVAL_FIELDS}
        for e in snap["entries"]
    }


def test_no_negative_interval_in_the_window_between_a_passing_and_its_race_info(state):
    """A ``$J`` landing before its ``$G`` must not push the whole field negative.

    ``captures/capture_20260517T134241.log`` lines 1777-1778 are one
    millisecond apart::

        $J,"15","00:01:04.711","00:15:38.661"
        $G,1,"15",14,"00:15:38.661"

    ``$J`` carries no lap number at all, so the leader's ``total_time``
    advanced to lap 14 while its ``laps`` stayed on 13, and the interval was
    taken by subtracting lap 13's baseline from lap 14's time.  The leader is
    the normalisation base, so the whole field shifted by its spurious
    +64.711: car 64 measured -11.605, and ``intervalText`` keeps the sign
    deliberately, so the page rendered it.  Broadcasting every 0.25 s against a
    ~1 ms window makes this rare per crossing and certain over a session — and
    it lands on every row at once.
    """
    _feed_three_cars_on_lap_13(state)
    settled = _intervals(state.snapshot())
    assert settled["64"]["diff_leader_seconds"] == pytest.approx(53.106, abs=1e-3)
    assert settled["88"]["diff_leader_seconds"] == pytest.approx(74.793, abs=1e-3)

    state.process({
        "type": "passing", "reg_number": "15",
        "lap_time": "00:01:04.711", "total_time": "00:15:38.661",
    })
    in_window = _intervals(state.snapshot())
    for reg, fields in in_window.items():
        for name, value in fields.items():
            assert value is None or value >= 0, f"{reg} {name} = {value}"
    assert in_window == settled

    state.process({
        "type": "race_info", "position": "1", "reg_number": "15",
        "laps": "14", "total_time": "00:15:38.661",
    })
    assert _intervals(state.snapshot()) == settled


def test_lap_info_advancing_a_lap_without_a_time_does_not_blank_the_table(state):
    """``$SP``/``$SR`` advance ``laps`` and write no cumulative time at all.

    Taking the lap from the live field, the leader moved to lap 14 with its
    ``total_time`` still on lap 13, so the lookup hit a lap the index had never
    recorded: the baseline was *None* and the interval pass returned early,
    blanking the entire table for a whole lap.  Worse, if a rival reached that
    lap first, every row rescaled against the leader's stale deficit —
    measured on these three cars as *None*, then 117.817 and 192.610.

    ``$SP``/``$SR`` appear in none of the eleven sample files, so this route is
    guarded on principle: the ``rmonitor-feed`` skill records them as real
    output from some Orbits setups that appears in no published spec.
    """
    _feed_three_cars_on_lap_13(state)
    settled = _intervals(state.snapshot())

    state.process({
        "type": "lap_info", "position": "1", "reg_number": "15",
        "lap_number": "14", "lap_time": "00:01:04.711",
    })
    after = _intervals(state.snapshot())
    assert after["64"]["diff_leader_seconds"] == pytest.approx(53.106, abs=1e-3)
    assert after["88"]["diff_leader_seconds"] == pytest.approx(74.793, abs=1e-3)
    assert after == settled


def test_lap_deficit_is_measured_from_the_stamped_pair_not_the_live_lap_count(state):
    """Both halves of a column are read from the same instant.

    With the ``+N L`` deficit taken from the live ``laps`` and the seconds from
    the stamped pair, a ``$SP`` lap advance drops a two-lap-down car to a
    one-lap deficit, which falls below the two-lap threshold and reports a
    seconds value describing the *earlier* lap.  The two halves of one column
    would then describe different instants.
    """
    _feed_lapped_field(state)
    entries = _by_reg(state.snapshot())
    assert entries["101"]["diff_leader_laps"] == 2
    assert entries["101"]["diff_leader_seconds"] is None

    state.process({
        "type": "lap_info", "reg_number": "101", "lap_number": "7",
    })
    entries = _by_reg(state.snapshot())
    assert entries["101"]["laps"] == "7"
    assert entries["101"]["diff_leader_laps"] == 2
    assert entries["101"]["diff_leader_seconds"] is None


def test_a_stale_race_info_replay_does_not_move_the_stamped_pair_backwards(state):
    """A stale ``$G`` replay leaves the stamped pair on the later lap.

    ``$G`` lap numbers do regress within a session, rarely: counting per file
    and clearing at each ``$I`` exactly as ``_init`` does, once across the
    eight captures (``captures/capture_20260419T090018.log``) and 11 / 1 / 1
    across the three ``examples/*.txt``.  Every one inspected is a stale replay
    of a *still-consistent* pair — ``examples/2009 Sebring Test ALMS Session 4
    - 0800-1000.txt`` line 4500 sends ``$G,6,"44",15,"00:51:28.997"`` after
    line 4499 sent lap 16, which is lap 15 carrying lap 15's own time — so a
    plain last-write would already report a correct interval, one lap old, for
    one snapshot.  The guard buys stability, not correctness.

    The stamped field is asserted directly because it is the only observable
    here: in this capture car 15's lap-13 time *is* the index minimum at lap
    13, so a pair moved backwards yields an identical interval.
    """
    _feed_capture_sequence(state)
    entries = _by_reg(state.snapshot())
    assert entries["15"]["timed_lap"] == 14
    assert entries["15"]["timed_lap_seconds"] == pytest.approx(938.661, abs=1e-3)

    state.process({
        "type": "race_info", "position": "1", "reg_number": "15",
        "laps": "13", "total_time": "00:14:33.950",
    })
    entries = _by_reg(state.snapshot())
    assert entries["15"]["timed_lap"] == 14
    assert entries["15"]["timed_lap_seconds"] == pytest.approx(938.661, abs=1e-3)
    # snapshot() has no field allowlist, so both reach the broadcast payload.
    assert entries["64"]["timed_lap"] == 13
    assert entries["88"]["timed_lap"] == 13


def test_qualifying_sentinel_best_lap_yields_no_gap_or_diff(state):
    """``00:59:59.999`` is a "no time set" marker, not a 3599.999 s lap.

    The race branch has always rejected it through the shared input guard,
    while the qualifying branch checked only that the parsed value was
    positive, so a sentinel best lap rendered ``+58:19.999``.  It matters more
    in qualifying than in a race: across the three ``examples/*.txt`` the
    sentinel appears 72 / 1581 / 712 times in ``$H`` against 37 / 162 / 625 in
    ``$G``, and ``examples/2009 Sebring Test ALMS Session 4 - 0800-1000.txt``
    *opens* with ``$H,1,"21",0,"00:59:59.999"`` — position 1 holding the
    sentinel, so at session start the whole field normalises against it and
    every row reads ``+0.000`` rather than ``—``.
    """
    for position, reg, best_lap_time in [
        ("1", "15", "00:01:40.000"),
        ("2", "64", "00:01:41.500"),
        ("3", "99", "00:59:59.999"),
        ("4", "77", ""),
    ]:
        state.process({
            "type": "qual_info", "position": position,
            "reg_number": reg, "best_lap_time": best_lap_time,
        })
    entries = _by_reg(state.snapshot())
    assert entries["64"]["diff_leader_seconds"] == pytest.approx(1.5, abs=1e-3)
    assert entries["64"]["gap_ahead_seconds"] == pytest.approx(1.5, abs=1e-3)
    for reg in ("15", "99", "77"):
        for name in _INTERVAL_FIELDS:
            assert entries[reg][name] is None, f"{reg} {name}"


def test_a_state_file_without_the_stamped_pair_restores_and_self_heals(state):
    """A store written before the pair existed restores blank, then repairs.

    ``_to_dict`` stores ``competitors`` wholesale and ``_load_dict`` restores
    them verbatim, so the pair persists with no serialisation code of its own —
    and a ``data/state.json`` from the previous release simply lacks both keys.
    Read with ``.get()`` they come back *None* and both columns render ``—``;
    the first ``$G`` per competitor repairs it.  Self-healing within a lap, so
    there is no migration to run before deploying.
    """
    import json

    _feed_capture_sequence(state)
    payload = json.loads(json.dumps(state._to_dict()))
    for competitor in payload["competitors"].values():
        del competitor["timed_lap"]
        del competitor["timed_lap_seconds"]

    restored = RaceState()
    restored._load_dict(payload)
    entries = _by_reg(restored.snapshot())
    for reg in ("15", "64", "88"):
        for name in _INTERVAL_FIELDS:
            assert entries[reg][name] is None, f"{reg} {name}"

    _feed_capture_sequence(restored)
    entries = _by_reg(restored.snapshot())
    assert entries["64"]["diff_leader_seconds"] == pytest.approx(53.106, abs=1e-3)
    assert entries["88"]["diff_leader_seconds"] == pytest.approx(74.793, abs=1e-3)


# ---------------------------------------------------------------------------
# Class codes from the relay's :51738 source
# ---------------------------------------------------------------------------

def _add_car(state, reg, *, number=None, transponder="", class_number="1"):
    state.process({
        "type": "competitor",
        "reg_number": reg,
        "number": number or reg,
        "transponder": transponder,
        "first_name": "Ann",
        "last_name": "Example",
        "nationality": "",
        "class_number": class_number,
    })


def _codes(state, run_id, *entries):
    return state.process({"type": "class_codes", "run_id": run_id, "entries": list(entries)})


def _code_entry(entrant, number, class_name, code, transponder=""):
    return {
        "entrant_id": entrant, "kind": "added", "number": number,
        "class_name": class_name, "transponder": transponder, "class_code": code,
    }


def _entry_for(snap, reg):
    return next(e for e in snap["entries"] if e["reg_number"] == reg)


def test_competitor_transponder_is_stored_and_not_blanked_by_comp(state):
    _add_car(state, "7", transponder="1234567")
    state.process({
        "type": "competitor", "reg_number": "7", "number": "7",
        "first_name": "Ann", "last_name": "Example", "class_number": "1",
        "nationality": "", "additional_data": "",
    })
    assert state.competitors["7"]["transponder"] == "1234567"


def test_class_codes_survive_init(state):
    assert _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Saloon Cup", "SC", "1234567")) == "class_codes"
    state.process({"type": "init"})
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7", transponder="1234567")
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"


def test_class_code_joined_by_transponder_even_after_a_renumber(state):
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "190", transponder="1234567")
    # The operator renumbered 190 to 90; the push side saw it first.
    _codes(state, "0x4000AAAA", _code_entry("e1", "90", "Saloon Cup", "SC", "1234567"))
    snap = state.snapshot()
    assert _entry_for(snap, "190")["class_code"] == "SC"
    assert snap["class_code_missing"] == 0


def test_an_entry_list_entry_gives_a_rental_transponder_car_its_code(state):
    # An entrant never edited while the relay was connected: no push carries
    # the code, and the run's entry list does, under the rental transponder's
    # name as the feed carries it.
    state.process({"type": "class_info", "unique_number": "1", "description": "KMSC Pre Injection 600"})
    _add_car(state, "100", transponder="NE10")
    _codes(state, "0x4000AAAA", {
        **_code_entry("a0000100", "100", "KMSC Pre Injection 600", "PI6", "NE10"),
        "kind": "entry list",
    })
    snap = state.snapshot()
    assert _entry_for(snap, "100")["class_code"] == "PI6"
    assert snap["class_code_missing"] == 0


def _entry_list(state, run_id, *entries):
    return state.process({
        "type": "class_codes", "run_id": run_id, "entry_list": True,
        "entries": [{**e, "kind": "entry list"} for e in entries],
    })


def test_an_entry_list_withdraws_a_code_its_runs_earlier_list_gave(state):
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7", transponder="1234567")
    _add_car(state, "8", transponder="7654321")
    _entry_list(state, "0x4000AAAA",
                _code_entry("a0000007", "7", "Saloon Cup", "SC", "1234567"),
                _code_entry("a0000008", "8", "Saloon Cup", "SC", "7654321"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"
    # The host cleared car 7's code, so the next list leaves it out.
    assert _entry_list(
        state, "0x4000aaaa", _code_entry("a0000008", "8", "Saloon Cup", "SC", "7654321")
    ) == "class_codes"
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert _entry_for(snap, "8")["class_code"] == "SC"
    # An empty list withdraws the rest.
    assert _entry_list(state, "0x4000AAAA") == "class_codes"
    assert state.class_codes == {}


def test_an_entry_list_leaves_pushes_and_other_runs_lists_alone(state):
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Saloon Cup", "SC", "1234567"))
    _entry_list(state, "0x4000BBBB", _code_entry("a0000009", "9", "Saloon Cup", "SC", "9"))
    _entry_list(state, "0x4000AAAA", _code_entry("a0000008", "8", "Saloon Cup", "SC", "8"))
    _entry_list(state, "0x4000AAAA")
    assert sorted(state.class_codes) == ["0x4000aaaa\te1", "0x4000bbbb\ta0000009"]
    # A plain batch never withdraws anything.
    _codes(state, "0x4000BBBB")
    assert "0x4000bbbb\ta0000009" in state.class_codes


def test_an_entry_list_replaces_a_push_under_its_run_id_spelled_in_either_case(state):
    """A start notice may spell the run id in lower case where pushes use
    upper; one entrant of one run must still be one record, or a car without
    a transponder sees two codes and gets none."""
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7")
    _codes(state, "0x400027FB", _code_entry("a0000007", "7", "Saloon Cup", "SC"))
    _entry_list(state, "0x400027fb", _code_entry("a0000007", "7", "Saloon Cup", "SD"))
    assert list(state.class_codes) == ["0x400027fb\ta0000007"]
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SD"


def test_a_store_holding_a_record_under_two_spellings_keeps_the_newer(state):
    rec = {"number": "7", "class_name": "Saloon Cup", "transponder": "", "class_code": "SC"}
    state.load_class_codes({"class_codes": {
        "0x400027FB\te1": {**rec, "received_at": time.time() - 60},
        "0x400027fb\te1": {**rec, "class_code": "SD", "received_at": time.time() - 120},
    }})
    assert list(state.class_codes) == ["0x400027fb\te1"]
    assert state.class_codes["0x400027fb\te1"]["class_code"] == "SC"


@pytest.mark.parametrize("bad", [
    {"run_id": "0x4000AAAA"},
    {"run_id": "0x4000AAAA", "entries": "not a list"},
    {"run_id": "0x4000AAAA", "entries": {"a0000007": {}}},
    {"run_id": "", "entries": []},
    {"run_id": "0x4000AAAA", "entries": [None]},
    {"run_id": "0x4000AAAA", "entries": [{"entrant_id": "a0000008", "class_code": ""}]},
    {"run_id": "0x4000AAAA", "entries": [], "entry_list": False},
    {"run_id": "0x4000AAAA", "entries": [], "entry_list": "yes"},
    {"run_id": "0x4000AAAA", "entries": [{"entrant_id": "a0000008", "class_code": "SC"}]},
    {"run_id": "0x4000AAAA", "entries": [
        {"entrant_id": "a0000008", "class_code": "SC", "number": "8"}]},
])
def test_a_malformed_entry_list_withdraws_nothing(state, bad):
    _entry_list(state, "0x4000AAAA", _code_entry("a0000007", "7", "Saloon Cup", "SC", "1"))
    before = dict(state.class_codes)
    state.process({"type": "class_codes", "entry_list": True} | bad)
    # A usable row may still be stored additively; nothing is withdrawn.
    assert state.class_codes.items() >= before.items()


def test_a_restored_entry_list_code_is_still_withdrawn_by_the_next_list(state):
    import json

    _entry_list(state, "0x4000AAAA", _code_entry("a0000007", "7", "Saloon Cup", "SC", "1"))
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_codes == state.class_codes
    _entry_list(restored, "0x4000AAAA")
    assert restored.class_codes == {}


def _aged(entry, seconds):
    return {**entry, "age_seconds": seconds}


def test_a_list_omitting_an_entrant_hides_its_older_sibling_push(state):
    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _car_in_class(state, "8", "7654321", SLW)
    _codes(state, DC, _aged(_code_entry("e7", "7", SLW, "SL", "1234567"), 600))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SL"
    _entry_list(state, DD, _code_entry("a0000008", "8", SLW, "SL", "7654321"))
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert _entry_for(snap, "8")["class_code"] == "SL"


def test_a_list_omitting_an_entrant_hides_the_running_runs_older_push_too(state):
    # A push's entrant id need not equal the list's car reg, so the list's
    # time, not a key match, supersedes it.
    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _car_in_class(state, "8", "7654321", SLW)
    _codes(state, DD, _aged(_code_entry("e7", "7", SLW, "SL", "1234567"), 600))
    _entry_list(state, DD, _code_entry("a0000008", "8", SLW, "SL", "7654321"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_an_empty_list_still_supersedes_an_older_sibling_push(state):
    """The relay drops codeless rows, so a list whose every code was cleared
    arrives with no rows; it is still the run's whole list, and dated."""
    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _car_in_class(state, "8", "7654321", SLW)
    _codes(state, DC, _aged(_code_entry("e7", "7", SLW, "F3", "1234567"), 600))
    _entry_list(state, DD, _code_entry("a0000008", "8", SLW, "SL", "7654321"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""
    assert _entry_list(state, DD) == "class_codes"
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert _entry_for(snap, "8")["class_code"] == ""


def test_a_retried_empty_list_is_dated_by_its_own_age_not_its_arrival(state):
    """A push made after the list was read but before a retry delivered it
    is a later edit, so the list must not supersede it."""
    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _codes(state, DC, _aged(_code_entry("e7", "7", SLW, "F3", "1234567"), 300))
    state.process({
        "type": "class_codes", "run_id": DD, "entry_list": True,
        "age_seconds": 600, "entries": [],
    })
    assert _entry_for(state.snapshot(), "7")["class_code"] == "F3"


def test_a_store_saved_before_list_dates_rebuilds_them_from_its_list_rows(state):
    """A store from before list dates were saved still supersedes on restart."""
    import json

    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _car_in_class(state, "8", "7654321", SLW)
    _codes(state, DC, _aged(_code_entry("e7", "7", SLW, "F3", "1234567"), 600))
    _entry_list(state, DD, _code_entry("a0000008", "8", SLW, "SL", "7654321"))
    data = json.loads(json.dumps(state.class_codes_to_dict()))
    del data["class_code_lists"]
    restored = RaceState()
    restored.load_class_codes(data)
    assert restored.class_code_lists == state.class_code_lists
    _session(restored, RACE_15, "15")
    _car_in_class(restored, "7", "1234567", SLW)
    assert _entry_for(restored.snapshot(), "7")["class_code"] == ""


def test_an_empty_lists_date_survives_a_save_and_restore(state):
    import json

    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _codes(state, DC, _aged(_code_entry("e7", "7", SLW, "F3", "1234567"), 600))
    _entry_list(state, DD)
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_lists == state.class_code_lists
    assert list(restored.class_code_lists) == [DD.lower()]
    _session(restored, RACE_15, "15")
    _car_in_class(restored, "7", "1234567", SLW)
    assert _entry_for(restored.snapshot(), "7")["class_code"] == ""


@pytest.mark.parametrize("lists", [
    "nonsense",
    {"0x400035dd": "yesterday"},
    {"0x400035dd": None},
    {"0x400035dd": float("nan")},
    {7: 1.0},
])
def test_a_malformed_list_date_is_dropped_without_losing_codes(state, lists):
    _codes(state, DC, _code_entry("e7", "7", SLW, "F3", "1234567"))
    data = state.class_codes_to_dict() | {"class_code_lists": lists}
    restored = RaceState()
    restored.load_class_codes(data)
    assert restored.class_code_lists == {}
    assert restored.class_codes == state.class_codes


def test_a_list_date_from_the_future_is_capped_at_now(state, monkeypatch):
    import server.race_state as rs

    monkeypatch.setattr(rs.time, "time", lambda: 1_000_000.0)
    state.load_class_codes({"class_code_lists": {"0x400035dd": 9_000_000_000.0}})
    assert state.class_code_lists == {"0x400035dd": 1_000_000.0}


def test_an_expired_list_date_is_pruned_and_dirties_the_state(state, monkeypatch):
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _entry_list(state, DD)
    assert list(state.class_code_lists) == [DD.lower()]
    revision = state.class_codes_revision
    state._dirty = False
    now[0] += rs._PUSHED_CODE_TTL_SECONDS + 1
    assert state.prune_expired_class_codes() is True
    assert state.class_code_lists == {}
    assert state.class_codes_revision > revision
    assert state._dirty


def test_a_list_still_supersedes_once_a_push_replaces_its_last_row(state):
    """A same-key push overwrites the list's only row; the list's date must
    outlive the row, here and across a restart."""
    import json

    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _car_in_class(state, "8", "7654321", SLW)
    _codes(state, DC, _aged(_code_entry("e7", "7", SLW, "F3", "1234567"), 600))
    _entry_list(state, DD, _aged(_code_entry("a0000008", "8", SLW, "SL", "7654321"), 300))
    _codes(state, DD, _code_entry("a0000008", "8", SLW, "SL C", "7654321"))
    assert not any(v.get("origin") for v in state.class_codes.values())
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert _entry_for(snap, "8")["class_code"] == "SL C"
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    _session(restored, RACE_15, "15")
    _car_in_class(restored, "7", "1234567", SLW)
    assert _entry_for(restored.snapshot(), "7")["class_code"] == ""


def test_the_registry_never_fills_a_code_the_running_runs_list_withholds(state):
    """The host's current list is the truth; a uniform registry class is not
    proof the car the list leaves uncoded has that code."""
    _runs_preload(
        state,
        _run_row(DC, GROUP_D3, "Warm Up"),
        _run_row(DD, GROUP_D3, RACE_15),
        entries=[_pre("1234567", SLW, "SC"), _pre("7654321", SLW, "SC")],
    )
    _session(state, RACE_15, "15")
    _started(state, DD, RACE_15)
    _car_in_class(state, "7", "1234567", SLW)
    _car_in_class(state, "8", "7654321", SLW)
    _codes(state, DC, _aged(_code_entry("e7", "7", SLW, "SX", "1234567"), 600))
    _entry_list(state, DD, _code_entry("a0000008", "8", SLW, "SC", "7654321"))
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert _entry_for(snap, "8")["class_code"] == "SC"


@pytest.mark.parametrize("older_push", [False, True])
def test_an_empty_list_leaves_no_code_source_so_no_missing_count(state, older_push):
    """The empty list suppresses the registry and older pushes, so nothing
    could fill a cell: the page must not count blanks as missing codes."""
    _runs_preload(
        state,
        _run_row(DC, GROUP_D3, "Warm Up"),
        _run_row(DD, GROUP_D3, RACE_15),
        entries=[_pre("1234567", SLW, "SC")],
    )
    _session(state, RACE_15, "15")
    _started(state, DD, RACE_15)
    _car_in_class(state, "7", "1234567", SLW)
    if older_push:
        _codes(state, DC, _aged(_code_entry("e7", "7", SLW, "SX", "1234567"), 600))
    assert state.snapshot()["class_codes_available"] is True
    _entry_list(state, DD)
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert snap["class_code_missing"] == 1
    assert snap["class_codes_available"] is False
    # A newer push is a source again.
    _codes(state, DC, _code_entry("e7", "7", SLW, "SC", "1234567"))
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == "SC"
    assert snap["class_codes_available"] is True


def test_a_push_newer_than_the_list_still_counts(state):
    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _entry_list(state, DD, _aged(_code_entry("a0000007", "7", SLW, "SL", "1234567"), 600))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SL"
    _codes(state, DC, _code_entry("e7", "7", SLW, "SL C", "1234567"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SL C"


def test_without_a_list_for_the_running_run_every_in_scope_push_counts(state):
    # Only the running run's own list stands for this session's entries.
    _race_15(state)
    _car_in_class(state, "7", "1234567", SLW)
    _codes(state, DD, _aged(_code_entry("e7", "7", SLW, "SL", "1234567"), 600))
    _entry_list(state, DC, _code_entry("a0000008", "8", SLW, "SL", "7654321"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SL"


def test_a_rental_car_takes_the_list_code_over_a_conflicting_older_push(state):
    _race_15(state)
    # The feed carries the rental's own transponder, the host's records the
    # owner's, so layer 1 finds nothing and layer 2 sees both codes unless
    # the list supersedes the older push.
    _car_in_class(state, "26", "NE5", SLW)
    _codes(state, DC, _aged(_code_entry("e26", "26", SLW, "F3", "3776411"), 600))
    _entry_list(state, DD, _code_entry("a0000026", "26", SLW, "SL C", "3776411"))
    assert _entry_for(state.snapshot(), "26")["class_code"] == "SL C"


def test_class_code_joined_by_exact_number_and_class_when_no_transponder(state):
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7")
    # The same entrant pushed under two run ids is still one distinct code.
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Saloon Cup", "SC"))
    _codes(state, "0x8000BBBB", _code_entry("e1", "7", "Saloon Cup", "SC"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"


def test_class_code_is_blank_when_number_and_class_match_disagreeing_codes(state):
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7")
    _codes(
        state, "0x4000AAAA",
        _code_entry("e1", "7", "Saloon Cup", "SC"),
        _code_entry("e2", "7", "Saloon Cup", "SCR"),
    )
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert snap["class_code_missing"] == 1


def test_class_code_transponder_match_requires_the_same_class(state, monkeypatch):
    """A driver in two classes on one transponder gets each class's own code."""
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7", transponder="1234567")
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Saloon Cup", "SC", "1234567"))
    now[0] += 60  # the later push is for the driver's other class
    _codes(state, "0x4000BBBB", _code_entry("e9", "44", "Hot Hatch", "HH", "1234567"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"
    # With only the other class's push, the transponder alone is not enough.
    state.class_codes = {k: v for k, v in state.class_codes.items() if v["class_code"] == "HH"}
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert snap["class_code_missing"] == 1


def test_class_code_is_never_matched_on_a_class_name_prefix(state):
    state.process({"type": "class_info", "unique_number": "1", "description": "Modsports A2"})
    _add_car(state, "7")
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Modsports A", "A"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_class_code_is_blank_not_the_description_when_unmatched(state):
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7", transponder="1234567")
    _add_car(state, "8", transponder="7654321")
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Saloon Cup", "SC", "1234567"))
    snap = state.snapshot()
    unmatched = _entry_for(snap, "8")
    assert unmatched["class_code"] == ""
    assert unmatched["class_description"] == "Saloon Cup"
    assert snap["class_code_missing"] == 1


def test_an_unknown_class_does_not_match_an_empty_class_name(state):
    _add_car(state, "7", class_number="9")  # no $C for class 9
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "", "SC"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_expired_class_codes_are_not_joined(state, monkeypatch):
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7", transponder="1234567")
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Saloon Cup", "SC", "1234567"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"
    now[0] += rs._PUSHED_CODE_TTL_SECONDS + 1
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert snap["class_codes_available"] is False


def test_a_class_code_is_dated_from_its_push_not_its_arrival(state, monkeypatch):
    """A retried batch that lands hours late keeps the push's age, not a fresh TTL."""
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    ttl = rs._PUSHED_CODE_TTL_SECONDS
    late = {**_code_entry("e1", "7", "Saloon Cup", "SC"), "age_seconds": ttl - 60}
    _codes(state, "r", late)
    (rec,) = state.class_codes.values()
    assert rec["received_at"] == now[0] - (ttl - 60)
    now[0] += 120  # two minutes on, it is past the TTL counted from the push
    assert state.snapshot()["class_codes_available"] is False
    # Already past the TTL on arrival: never stored.
    stale = {**_code_entry("e2", "8", "Saloon Cup", "SC"), "age_seconds": ttl + 1}
    assert _codes(state, "r", stale) is None
    assert state.class_codes == {}


@pytest.mark.parametrize("age", [None, "soon", -5, float("nan"), float("inf"), True, [1]])
def test_an_unusable_class_code_age_counts_as_zero(state, monkeypatch, age):
    import server.race_state as rs

    monkeypatch.setattr(rs.time, "time", lambda: 1_000_000.0)
    _codes(state, "r", {**_code_entry("e1", "7", "Saloon Cup", "SC"), "age_seconds": age})
    (rec,) = state.class_codes.values()
    assert rec["received_at"] == 1_000_000.0


def test_malformed_class_codes_entries_are_skipped(state):
    assert state.process({"type": "class_codes", "run_id": "r", "entries": "nonsense"}) is None
    assert state.process({"type": "class_codes", "run_id": "r"}) is None
    assert _codes(
        state, "r",
        "not a dict",
        ["nor", "this"],
        _code_entry("e1", "7", "Saloon Cup", ""),
        _code_entry("", "7", "Saloon Cup", "SC"),
    ) is None
    assert state.class_codes == {}
    assert _codes(state, "r", {"entrant_id": 5, "number": 7, "class_code": "SC",
                               "class_name": None, "transponder": 1234567}) == "class_codes"
    (rec,) = state.class_codes.values()
    assert (rec["number"], rec["class_name"], rec["transponder"]) == ("7", "", "1234567")


def test_class_codes_round_trip_through_their_own_dict(state):
    import json

    state.process({"type": "class_info", "unique_number": "1", "description": "Saloon Cup"})
    _add_car(state, "7", transponder="1234567")
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Saloon Cup", "SC", "1234567"))
    restored = RaceState()
    restored._load_dict(json.loads(json.dumps(state._to_dict())))
    assert restored.class_codes == {}  # not part of the race-state dict
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_codes == state.class_codes
    assert _entry_for(restored.snapshot(), "7")["class_code"] == "SC"


def test_load_class_codes_tolerates_a_malformed_store(state):
    state.load_class_codes({"class_codes": "nonsense"})
    assert state.class_codes == {}
    state.load_class_codes({"class_codes": {
        "r\te1": {"number": "7", "received_at": "yesterday"},
        "r\te2": "not a dict",
        "r\te3": {"number": "7", "class_name": "Saloon Cup", "transponder": "",
                  "class_code": "SC", "received_at": __import__("time").time()},
    }})
    assert list(state.class_codes) == ["r\te3"]


@pytest.mark.parametrize("stamp", ["NaN", "Infinity", "-Infinity", "true", "1" + "0" * 400])
def test_load_class_codes_drops_a_non_finite_timestamp(state, stamp):
    """``json`` decodes these, and a NaN or infinite stamp would never expire."""
    import json

    raw = (
        '{"class_codes": {"r\\te1": {"number": "7", "class_name": "Saloon Cup",'
        ' "transponder": "1234567", "class_code": "SC", "received_at": %s}}}' % stamp
    )
    state.load_class_codes(json.loads(raw))
    assert state.class_codes == {}


def test_load_class_codes_caps_a_future_timestamp_at_now(state, monkeypatch):
    import server.race_state as rs

    monkeypatch.setattr(rs.time, "time", lambda: 1_000_000.0)
    state.load_class_codes({"class_codes": {"r\te1": {
        "number": "7", "class_name": "Saloon Cup", "transponder": "",
        "class_code": "SC", "received_at": 9_000_000_000.0,
    }}})
    assert state.class_codes["r\te1"]["received_at"] == 1_000_000.0


def test_class_codes_outlive_a_race_state_store_past_its_max_age(state, tmp_path, monkeypatch):
    """A restart after a quiet gap drops the race state but keeps the codes.

    The race-state store is discarded whole past ``STATE_MAX_AGE``; codes pushed
    for runs not yet started are never pushed again, so they live in a store of
    their own that only their per-entry TTL expires.
    """
    import server.race_state as rs
    import server.state_store as ss
    from server.state_store import JsonFileStateStore

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    monkeypatch.setattr(ss.time, "time", lambda: now[0])
    race_store = JsonFileStateStore(tmp_path / "state.json", max_age_seconds=900)
    codes_store = JsonFileStateStore(tmp_path / "state-class-codes.json")
    _add_car(state, "7", transponder="1234567")
    _codes(state, "r", _code_entry("e1", "7", "Saloon Cup", "SC", "1234567"))
    race_store.save(state._to_dict())
    codes_store.save(state.class_codes_to_dict())

    now[0] += 3600  # an hour between sessions, then a restart
    restored = RaceState()
    assert race_store.load() == {}
    restored.load_class_codes(codes_store.load())
    assert len(restored.class_codes) == 1

    now[0] += rs._PUSHED_CODE_TTL_SECONDS  # the entry's own expiry still applies
    later = RaceState()
    later.load_class_codes(codes_store.load())
    assert later.class_codes == {}


@pytest.mark.parametrize("code", [None, "", "absent"])
def test_load_class_codes_skips_a_record_without_a_code(state, code):
    """Live ingest never stores a codeless record, so a restore must not either:
    one would make ``class_codes_available`` true with no usable code."""
    rec = {"number": "7", "class_name": "Saloon Cup", "transponder": "",
           "received_at": __import__("time").time()}
    if code != "absent":
        rec["class_code"] = code
    state.load_class_codes({"class_codes": {"r\te1": rec}})
    assert state.class_codes == {}
    assert state.snapshot()["class_codes_available"] is False


def test_class_codes_do_not_make_stale_race_state_look_fresh(state, monkeypatch):
    """``last_updated`` dates the race state for ``STATE_MAX_AGE``; a class-code
    push arriving long after the race went quiet must not reset that age."""
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _add_car(state, "7")
    stale = state.last_updated
    now[0] += 3600
    state.mark_clean()
    assert _codes(state, "r", _code_entry("e1", "7", "Saloon Cup", "SC")) == "class_codes"
    assert state.last_updated == stale
    assert state._to_dict()["last_updated"] == stale
    assert state.dirty  # still broadcast


def test_snapshot_reports_class_codes_available(state):
    assert state.snapshot()["class_codes_available"] is False
    _codes(state, "r", _code_entry("e1", "7", "Saloon Cup", "SC"))
    assert state.snapshot()["class_codes_available"] is True


def test_a_class_codes_batch_is_logged_with_its_stored_count(state, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="server.race_state")
    _codes(state, "0x4000AAAA", _code_entry("e1", "7", "Saloon Cup", "SC"),
           _code_entry("e2", "8", "Saloon Cup", ""))
    assert "Class codes for run 0x4000AAAA: stored 1 of 2 entries" in caplog.text
    # A batch of codeless records stores nothing, and is logged all the same.
    assert _codes(state, "0x4000BBBB", _code_entry("e3", "9", "Saloon Cup", "")) is None
    assert "Class codes for run 0x4000BBBB: stored 0 of 1 entries" in caplog.text


# ---------------------------------------------------------------------------
# The registry preload
# ---------------------------------------------------------------------------

def _preload(state, *entries, age_seconds=0.0):
    return state.process({
        "type": "class_code_preload", "entries": list(entries), "age_seconds": age_seconds,
    })


def _pre(transponder, class_name, code, *, reg="", number=""):
    return {
        "transponder": transponder, "class_name": class_name, "class_code": code,
        "registration_id": reg, "number": number,
    }


def _car_in_class(state, reg, transponder, description, *, number=None, class_number="1"):
    state.process({"type": "class_info", "unique_number": class_number, "description": description})
    _add_car(state, reg, number=number, transponder=transponder, class_number=class_number)


def test_preload_code_is_used_when_the_class_is_uniform(state):
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    assert _preload(state, _pre("1234567", "Saloon Cup", "SC"),
                    _pre("7654321", "Saloon Cup", "SC")) == "class_codes"
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == "SC"
    assert snap["class_code_missing"] == 0


def test_preload_code_is_withheld_when_the_class_name_spans_several_codes(state):
    """The registry holds a registered code that a meeting entry can override."""
    _car_in_class(state, "7", "1234567", "Legends")
    _preload(state, _pre("1234567", "Legends", "LG"), _pre("7654321", "Legends", "LX"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_preload_code_is_withheld_when_the_transponder_has_two_codes_in_the_class(state):
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"), _pre("1234567", "Saloon Cup", "SX"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_preload_requires_an_exact_class_name(state):
    _car_in_class(state, "7", "1234567", "Saloon Cup A")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_a_pushed_code_overrides_the_preload_by_transponder(state):
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    _codes(state, "r", _code_entry("e1", "7", "Saloon Cup", "SP", "1234567"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SP"


def test_a_pushed_code_overrides_the_preload_by_number(state):
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    _codes(state, "r", _code_entry("e1", "7", "Saloon Cup", "SP"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SP"


def test_preload_is_matched_by_number_and_class_when_the_car_has_no_transponder(state):
    _car_in_class(state, "7", "", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC", reg="r1", number="7"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"


def test_preload_resolves_a_stale_transponder_by_number_and_class(state):
    """The car races on a transponder its registration does not hold yet;
    number and class still name exactly one registration."""
    _car_in_class(state, "4", "10484382", "Jaguar (A)")
    _preload(state, _pre("1184258", "Jaguar (A)", "A", reg="509ff32b", number="4"),
             _pre("5550001", "Jaguar (A)", "A", reg="r2", number="5"))
    assert _entry_for(state.snapshot(), "4")["class_code"] == "A"


def test_preload_is_blank_when_transponder_and_number_name_different_registrations(state):
    """A transponder that changed hands: it names another driver's
    registration, the number names the car's own, so neither is trusted."""
    _car_in_class(state, "26", "221241", "Saloon Cup")
    _preload(state, _pre("9990001", "Saloon Cup", "SC", reg="own", number="26"),
             _pre("221241", "Saloon Cup", "SC", reg="other", number="31"))
    assert _entry_for(state.snapshot(), "26")["class_code"] == ""


def test_preload_number_match_needs_exactly_one_registration(state):
    _car_in_class(state, "7", "", "Saloon Cup")
    _preload(state, _pre("1111111", "Saloon Cup", "SC", reg="r1", number="7"),
             _pre("2222222", "Saloon Cup", "SC", reg="r2", number="7"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_preload_with_an_ambiguous_number_falls_back_to_the_transponder(state):
    _car_in_class(state, "7", "1111111", "Saloon Cup")
    _preload(state, _pre("1111111", "Saloon Cup", "SC", reg="r1", number="7"),
             _pre("2222222", "Saloon Cup", "SC", reg="r2", number="7"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"


def test_preload_number_match_is_still_under_the_class_uniform_guard(state):
    _car_in_class(state, "7", "", "Guest")
    _preload(state, _pre("1111111", "Guest", "CI", reg="r1", number="7"),
             _pre("2222222", "Guest", "CB", reg="r2", number="8"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_a_preload_without_registration_ids_still_joins_by_transponder(state):
    """An older relay sends only the transponder, class and code."""
    old = {"transponder": "1234567", "class_name": "Saloon Cup", "class_code": "SC"}
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _car_in_class(state, "8", "", "Saloon Cup")
    assert _preload(state, old) == "class_codes"
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == "SC"
    assert _entry_for(snap, "8")["class_code"] == ""


def test_load_class_codes_accepts_an_old_format_preload(state):
    import time as _time

    old = {"transponder": "1234567", "class_name": "Saloon Cup", "class_code": "SC"}
    state.load_class_codes({"preload": {"received_at": _time.time(), "entries": [old]}})
    assert state.class_code_preload["entries"] == [_pre("1234567", "Saloon Cup", "SC")]
    assert state.class_code_preload["runs"] == []
    # Saved before the run table had its own date: it shares the records'.
    pre = state.class_code_preload
    assert pre["runs_received_at"] == pre["received_at"]
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"


def test_an_expired_preload_is_not_joined(state, monkeypatch):
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    now[0] += rs._CLASS_CODE_TTL_SECONDS + 1
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert snap["class_codes_available"] is False


def test_a_preloads_run_table_outlives_its_registry_for_day_two(state, monkeypatch):
    """The relay pulls the preload only on connect; one held overnight must
    still scope a day-2 run to its group, as the pushes it admits live 36 h."""
    import json

    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _runs_preload(
        state,
        _run_row(DC, GROUP_D3, "Warm Up"),
        _run_row(DD, GROUP_D3, RACE_15),
        entries=[_pre("7654321", SLW, "SL")],
    )
    _codes(state, DC, _code_entry("e7", "7", SLW, "F3", "1234567"))
    # Between the two lifetimes: the records have expired, the run table not.
    now[0] += (rs._CLASS_CODE_TTL_SECONDS + rs._PUSHED_CODE_TTL_SECONDS) / 2
    revision = state.class_codes_revision
    state._dirty = False
    assert state.prune_expired_class_codes() is True
    assert state.class_codes_revision > revision and state._dirty
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    for s in (state, restored):
        _session(s, RACE_15, "15")
        _started(s, DD, RACE_15)
        assert s._class_code_scope() == (DD, frozenset({DC.lower(), DD.lower(), GROUP_D3.lower()}))
        _car_in_class(s, "7", "1234567", SLW)
        _car_in_class(s, "8", "7654321", SLW)
        snap = s.snapshot()
        assert _entry_for(snap, "7")["class_code"] == "F3"
        # The registry itself is past its 12 h.
        assert _entry_for(snap, "8")["class_code"] == ""
    # Past the pushes' own TTL the run table goes too.
    now[0] += rs._PUSHED_CODE_TTL_SECONDS
    assert state.prune_expired_class_codes() is True
    assert state.class_code_preload["runs"] == []


def _runs_only_preload(state, *runs, age_seconds=0.0):
    return state.process({
        "type": "class_code_preload", "entries": [], "runs": list(runs),
        "age_seconds": age_seconds,
    })


def test_a_runs_only_preload_gives_the_running_run_its_group_scope(state):
    """A pull whose registry held no usable record still carries the run
    table, without which a known running run scopes to itself alone."""
    assert _runs_only_preload(
        state, _run_row(DC, GROUP_D3, "Warm Up"), _run_row(DD, GROUP_D3, RACE_15),
    ) == "class_codes"
    assert state.class_code_preload["entries"] == []
    assert state.class_code_preload["received_at"] is None
    _session(state, RACE_15, "15")
    _started(state, DD, RACE_15)
    assert state._class_code_scope()[1] == frozenset({DC.lower(), DD.lower(), GROUP_D3.lower()})
    _car_in_class(state, "7", "1234567", SLW)
    _codes(state, DC, _code_entry("e7", "7", SLW, "SL", "1234567"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SL"


def test_a_preloads_records_and_run_table_are_dated_by_their_own_ages(state, monkeypatch):
    """The relay can send an undelivered pull's records with a newer pull's
    run table; stale records must neither shorten nor discard fresh runs."""
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    runs = [_run_row(DC, GROUP_D3, "Warm Up"), _run_row(DD, GROUP_D3, RACE_15)]
    msg = {
        "type": "class_code_preload", "entries": [_pre("1234567", "Saloon Cup", "SC")],
        "runs": runs, "age_seconds": 3600.0, "runs_age_seconds": 60.0,
    }
    assert state.process(dict(msg)) == "class_codes"
    pre = state.class_code_preload
    assert pre["received_at"] == now[0] - 3600.0
    assert pre["runs_received_at"] == now[0] - 60.0
    # Records past the TTL are dropped alone; the fresh run table is kept.
    stale = dict(msg, age_seconds=rs._CLASS_CODE_TTL_SECONDS + 1)
    state = RaceState()
    assert state.process(stale) == "class_codes"
    assert state.class_code_preload["entries"] == []
    assert len(state.class_code_preload["runs"]) == 2
    assert state.class_code_preload["runs_received_at"] == now[0] - 60.0


def test_a_runs_only_preload_keeps_the_prior_records_without_renewing_them(
    state, monkeypatch
):
    import server.race_state as rs

    start = 1_000_000.0
    now = [start]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    now[0] += 6 * 3600
    revision = state.class_codes_revision
    state.mark_clean()
    assert _runs_only_preload(
        state, _run_row(DC, GROUP_D3, "Warm Up"), _run_row(DD, GROUP_D3, RACE_15),
        age_seconds=60.0,
    ) == "class_codes"
    assert state.class_codes_revision > revision and state.dirty
    pre = state.class_code_preload
    assert pre["entries"] == [_pre("1234567", "Saloon Cup", "SC")]
    assert pre["received_at"] == start
    runs_at = now[0] - 60.0
    assert pre["runs_received_at"] == runs_at
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"
    # The records still go 12 h after their own pull, the run table stays.
    now[0] = start + rs._CLASS_CODE_TTL_SECONDS + 1
    assert state.prune_expired_class_codes() is True
    assert state.prune_expired_class_codes() is False
    assert state.class_code_preload["entries"] == []
    assert len(state.class_code_preload["runs"]) == 2
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""
    # The run table lives 36 h from its own pull.
    now[0] = runs_at + rs._PUSHED_CODE_TTL_SECONDS
    assert state.prune_expired_class_codes() is False
    assert len(state.class_code_preload["runs"]) == 2
    now[0] += 1
    assert state.prune_expired_class_codes() is True
    assert state.class_code_preload == rs._preload_store([], None)
    assert state.prune_expired_class_codes() is False


def test_a_runs_only_preload_with_no_records_expires_with_its_run_table(state, monkeypatch):
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _runs_only_preload(state, _run_row(DC, GROUP_D3, "Warm Up"))
    now[0] += rs._CLASS_CODE_TTL_SECONDS + 1
    assert state.prune_expired_class_codes() is False
    assert state.class_code_preload["runs"] == [_run_row(DC, GROUP_D3, "Warm Up")]
    now[0] = 1_000_000.0 + rs._PUSHED_CODE_TTL_SECONDS + 1
    assert state.prune_expired_class_codes() is True
    assert state.class_code_preload == rs._preload_store([], None)


def test_a_preload_with_neither_records_nor_runs_is_ignored(state):
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    before = state.class_code_preload
    assert _runs_only_preload(state) is None
    assert state.class_code_preload is before


def test_a_runs_only_preload_round_trips_through_the_class_code_store(state):
    import json

    _runs_only_preload(state, _run_row(DC, GROUP_D3, "Warm Up"), age_seconds=60.0)
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_preload == state.class_code_preload
    assert restored.class_code_preload["runs"] == [_run_row(DC, GROUP_D3, "Warm Up")]


def test_the_run_tables_own_date_round_trips(state, monkeypatch):
    import json

    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    now[0] += 3600
    _runs_only_preload(state, _run_row(DC, GROUP_D3, "Warm Up"))
    saved = json.loads(json.dumps(state.class_codes_to_dict()))
    assert saved["preload"]["runs_received_at"] == now[0]
    restored = RaceState()
    restored.load_class_codes(saved)
    assert restored.class_code_preload == state.class_code_preload
    assert restored.class_code_preload["received_at"] == 1_000_000.0
    assert restored.class_code_preload["runs_received_at"] == now[0]


@pytest.mark.parametrize("runs_received_at, kept", [
    ("nonsense", False),
    (float("inf"), False),
    (2_000_000.0, True),
])
def test_load_class_codes_checks_the_run_tables_date(
    state, monkeypatch, runs_received_at, kept
):
    import server.race_state as rs

    monkeypatch.setattr(rs.time, "time", lambda: 1_000_000.0)
    state.load_class_codes({"preload": {
        "received_at": 1_000_000.0, "entries": [_pre("1234567", "Saloon Cup", "SC")],
        "runs": [_run_row()], "runs_received_at": runs_received_at,
    }})
    assert state.class_code_preload["entries"] == [_pre("1234567", "Saloon Cup", "SC")]
    assert state.class_code_preload["runs"] == ([_run_row()] if kept else [])
    if kept:
        # A future date is capped at now.
        assert state.class_code_preload["runs_received_at"] == 1_000_000.0


def test_a_preload_is_dated_from_its_pull_not_its_arrival(state, monkeypatch):
    import server.race_state as rs

    monkeypatch.setattr(rs.time, "time", lambda: 1_000_000.0)
    _preload(state, _pre("1234567", "Saloon Cup", "SC"), age_seconds=600.0)
    assert state.class_code_preload["received_at"] == 1_000_000.0 - 600.0
    # One already past the TTL is not stored at all.
    assert _preload(state, _pre("7654321", "Saloon Cup", "SX"),
                    age_seconds=rs._CLASS_CODE_TTL_SECONDS + 1) is None
    assert state.class_code_preload["entries"] == [_pre("1234567", "Saloon Cup", "SC")]


def test_a_new_preload_replaces_the_previous_one(state):
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    _preload(state, _pre("7654321", "Saloon Cup", "SC"))
    assert state.class_code_preload["entries"] == [_pre("7654321", "Saloon Cup", "SC")]
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_malformed_preload_entries_are_skipped(state):
    assert _preload(state, "not a dict", _pre("", "Saloon Cup", "SC"),
                    _pre("0", "Saloon Cup", "SC"), _pre("11", "", "SC"),
                    {"transponder": 44, "class_name": "Saloon Cup", "class_code": "SC"},
                    ) == "class_codes"
    assert state.class_code_preload["entries"] == [_pre("44", "Saloon Cup", "SC")]
    # Nothing usable leaves the previous preload in place.
    assert _preload(state, _pre("0", "Saloon Cup", "SX")) is None
    assert state.process({"type": "class_code_preload", "entries": "nonsense"}) is None
    assert state.class_code_preload["entries"] == [_pre("44", "Saloon Cup", "SC")]


def test_preload_survives_init(state):
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    state.process({"type": "init"})
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"


def test_preload_does_not_make_stale_race_state_look_fresh(state, monkeypatch):
    import server.race_state as rs

    stale = state.last_updated
    monkeypatch.setattr(rs.time, "time", lambda: stale + 3600)
    assert _preload(state, _pre("1234567", "Saloon Cup", "SC")) == "class_codes"
    assert state.last_updated == stale


def test_snapshot_reports_class_codes_available_from_a_preload_alone(state):
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    assert state.class_codes == {}
    assert state.snapshot()["class_codes_available"] is True


def test_preload_round_trips_through_the_class_code_store(state):
    import json

    state.process({
        "type": "class_code_preload",
        "entries": [_pre("1234567", "Saloon Cup", "SC", reg="r1", number="7")],
        "runs": [_run_row()],
        "age_seconds": 60.0,
    })
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_preload == state.class_code_preload
    assert restored.class_code_preload["runs"] == [_run_row()]
    _car_in_class(restored, "7", "", "Saloon Cup")
    assert _entry_for(restored.snapshot(), "7")["class_code"] == "SC"


@pytest.mark.parametrize("preload", [
    "nonsense",
    {"received_at": "yesterday", "entries": [_pre("1", "Saloon Cup", "SC")]},
    {"received_at": float("nan"), "entries": [_pre("1", "Saloon Cup", "SC")]},
    {"received_at": 1.0e9, "entries": "nonsense"},
    {"received_at": 1.0e9, "entries": [None, {"transponder": "1"}]},
])
def test_load_class_codes_tolerates_a_malformed_preload(state, preload):
    import time as _time

    pushed = {"r\te1": {"number": "7", "class_name": "Saloon Cup", "transponder": "",
                        "class_code": "SC", "received_at": _time.time()}}
    state.load_class_codes({"class_codes": pushed, "preload": preload})
    assert state.class_code_preload["entries"] == []
    assert list(state.class_codes) == ["r\te1"]


def test_load_class_codes_without_a_preload_key_still_loads_pushes(state):
    import time as _time

    state.load_class_codes({"class_codes": {"r\te1": {
        "number": "7", "class_name": "Saloon Cup", "transponder": "",
        "class_code": "SC", "received_at": _time.time(),
    }}})
    assert list(state.class_codes) == ["r\te1"]
    assert state.class_code_preload["entries"] == []


def test_preload_code_is_withheld_when_pushes_show_another_code_in_the_class(state):
    """A meeting's pushes are its own entries; one with another code in the
    class shows the registry's uniform code may be overridden here."""
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"), _pre("7654321", "Saloon Cup", "SC"))
    _codes(state, "r", _code_entry("e2", "8", "Saloon Cup", "SX", "7654321"))
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""
    # A push agreeing with the registry leaves the preload usable.
    other = RaceState()
    _car_in_class(other, "7", "1234567", "Saloon Cup")
    _preload(other, _pre("1234567", "Saloon Cup", "SC"), _pre("7654321", "Saloon Cup", "SC"))
    _codes(other, "r", _code_entry("e2", "8", "Saloon Cup", "SC", "7654321"))
    assert _entry_for(other.snapshot(), "7")["class_code"] == "SC"


def test_a_codeless_preload_record_withholds_its_class(state):
    """The guard needs every record in the class to carry the code; a codeless
    one does not, so the class is not shown as uniform."""
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"),
             {"transponder": "7654321", "class_name": "Saloon Cup"})
    assert state.class_code_preload["entries"][1] == _pre("7654321", "Saloon Cup", "")
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""


def test_a_codeless_preload_record_is_never_shown_as_a_code(state):
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", ""))
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == ""
    assert snap["class_codes_available"] is False


def test_a_codeless_preload_record_round_trips(state):
    import json

    _preload(state, _pre("1234567", "Saloon Cup", "SC"), _pre("7654321", "Saloon Cup", ""))
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_preload == state.class_code_preload


def test_expiry_of_either_store_dirties_the_state(state, monkeypatch):
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _codes(state, "r", _code_entry("e1", "7", "Saloon Cup", "SC"))
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    state.mark_clean()
    assert state.prune_expired_class_codes() is False
    assert not state.dirty
    now[0] += rs._CLASS_CODE_TTL_SECONDS + 1
    assert state.prune_expired_class_codes() is True
    assert state.dirty
    assert state.class_code_preload["entries"] == []
    assert state.class_codes != {}
    state.mark_clean()
    now[0] = 1_000_000.0 + rs._PUSHED_CODE_TTL_SECONDS + 1
    assert state.prune_expired_class_codes() is True
    assert state.dirty
    assert state.class_codes == {}


def test_an_expiry_found_by_a_snapshot_dirties_the_state(state, monkeypatch):
    """A new client's full snapshot must not expire a code silently for the rest."""
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    state.mark_clean()
    now[0] += rs._CLASS_CODE_TTL_SECONDS + 1
    state.snapshot()
    assert state.dirty


def test_class_codes_revision_moves_only_when_a_store_changes(state, monkeypatch):
    """The periodic save rewrites the class-code store only when this moves."""
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    revisions = [state.class_codes_revision]

    def moved():
        revisions.append(state.class_codes_revision)
        return revisions[-1] != revisions[-2]

    _codes(state, "r", _code_entry("e1", "7", "Saloon Cup", "SC"))
    assert moved()
    _codes(state, "r", _code_entry("e2", "8", "Saloon Cup", ""))  # nothing stored
    assert not moved()
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    assert moved()
    _preload(state, _pre("0", "Saloon Cup", "SC"))  # nothing usable
    assert not moved()
    state.snapshot()
    state.process({"type": "init"})
    assert not moved()
    now[0] += rs._CLASS_CODE_TTL_SECONDS + 1
    state.prune_expired_class_codes()
    assert moved()


# ---------------------------------------------------------------------------
# Scoping pushes to the running run
# ---------------------------------------------------------------------------

def _run_row(run_id="0x40002806", group_id="0x80000985", name="Race 7 - 2nd Race"):
    return {"run_id": run_id, "group_id": group_id, "name": name}


def _started(state, run_id="0x40002806", name="Race 7 - 2nd Race", age_seconds=0.0):
    return state.process({
        "type": "class_code_run", "run_id": run_id, "name": name, "age_seconds": age_seconds,
    })


def _session(state, description="Race 7 - 2nd Race", number="27"):
    state.process({"type": "run", "unique_number": number, "description": description})


def _runs_preload(state, *runs, entries=()):
    # A preload needs one usable entry to be stored at all.
    entries = list(entries) or [_pre("9999999", "Unused Class", "UC")]
    return state.process({"type": "class_code_preload", "entries": entries, "runs": list(runs)})


def test_pushes_for_another_run_are_ignored_once_the_run_is_known(state):
    """A remote operator's post-race edit to a finished run is not this session."""
    _session(state)
    _started(state)
    _car_in_class(state, "72", "5588219", "Classic K")
    _codes(state, "0x40002804", _code_entry("e72", "72", "Classic K", "CM", "5588219"))
    _codes(state, "0x80000000", _code_entry("e72", "72", "Classic K", "CM", "5588219"))
    snap = state.snapshot()
    assert snap["class_code_scope"] == "0x40002806"
    assert _entry_for(snap, "72")["class_code"] == ""
    _codes(state, "0x40002806", _code_entry("e72", "72", "Classic K", "CM", "5588219"))
    assert _entry_for(state.snapshot(), "72")["class_code"] == "CM"


def test_group_tagged_pushes_count_for_the_running_run(state):
    _runs_preload(state, _run_row())
    _session(state)
    _started(state)
    _car_in_class(state, "4", "10484382", "Jaguar (A)")
    _codes(state, "0x80000985", _code_entry("e4", "4", "Jaguar (A)", "A", "10484382"))
    assert _entry_for(state.snapshot(), "4")["class_code"] == "A"


def test_scoped_out_pushes_take_no_part_in_the_guard(state):
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _preload(state, _pre("1234567", "Saloon Cup", "SC"))
    _codes(state, "0x40002804", _code_entry("e8", "8", "Saloon Cup", "SX", "7654321"))
    # Unscoped, the other run's push shows the class is not uniform.
    assert _entry_for(state.snapshot(), "7")["class_code"] == ""
    _session(state)
    _started(state)
    assert _entry_for(state.snapshot(), "7")["class_code"] == "SC"


def test_scope_follows_the_run_description_not_a_stale_started_run(state):
    _started(state, "0x40002805", "Race 6 - AMENDED GRID")
    _session(state, "Race 7 - 2nd Race")
    assert state.snapshot()["class_code_scope"] == ""
    _runs_preload(state, _run_row())
    assert state.snapshot()["class_code_scope"] == "0x40002806"


def test_scope_from_the_run_table_picks_the_newest_run_with_that_name(state):
    _runs_preload(
        state,
        _run_row("0x40001000", "0x80000101"),
        _run_row("0x40002806", "0x80000985"),
        _run_row("0x400015E8", "0x80000050"),
        _run_row("0x40002807", "0x80000984", "Race 8"),
    )
    _session(state)
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    assert state._class_code_scope()[1] == frozenset({"0x40002806", "0x80000985"})


# One class's meeting as the timing host groups it: the warm-up whose grid
# edits were pushed, the race they feed, and another class's warm-up.
DC, DD, E1 = "0x400035DC", "0x400035DD", "0x400035E1"
GROUP_D3 = "0x800009D3"
RACE_15 = "Race 15 - 2nd Race"
SLW = "Scottish Lightweights"


def _race_15(state):
    _runs_preload(
        state,
        _run_row(DC, GROUP_D3, "Warm Up"),
        _run_row(DD, GROUP_D3, RACE_15),
        _run_row(E1, "0x800009D4", "Warm Up"),
    )
    _session(state, RACE_15, "15")
    _started(state, DD, RACE_15)


def test_scope_takes_in_every_run_of_the_running_runs_group(state):
    _race_15(state)
    assert state._class_code_scope()[1] == frozenset({"0x400035dc", "0x400035dd", "0x800009d3"})
    _car_in_class(state, "7", "1234567", SLW)
    _car_in_class(state, "8", "7654321", SLW)
    _codes(state, DC, _code_entry("e7", "7", SLW, "SL", "1234567"))
    _codes(state, E1, _code_entry("e8", "8", SLW, "SL", "7654321"))
    snap = state.snapshot()
    assert snap["class_code_scope"] == DD
    assert _entry_for(snap, "7")["class_code"] == "SL"
    assert _entry_for(snap, "8")["class_code"] == ""


def test_a_sibling_push_conflict_resolves_by_transponder_to_the_latest(state):
    _race_15(state)
    _car_in_class(state, "461", "3456789", SLW)
    _codes(state, DC, {**_code_entry("e461", "461", SLW, "F3", "3456789"), "age_seconds": 960})
    _codes(state, DD, _code_entry("e461", "461", SLW, "SL C", "3456789"))
    assert _entry_for(state.snapshot(), "461")["class_code"] == "SL C"


def test_a_sibling_push_conflict_without_a_usable_transponder_stays_blank(state):
    # A rental: the feed carries the transponder's name, the pushes its number.
    _race_15(state)
    _car_in_class(state, "26", "NE5", SLW)
    _codes(state, DC, {**_code_entry("e26", "26", SLW, "F3", "3776411"), "age_seconds": 960})
    _codes(state, DD, _code_entry("e26", "26", SLW, "SL C", "3776411"))
    assert _entry_for(state.snapshot(), "26")["class_code"] == ""


def test_a_run_missing_from_the_table_scopes_to_itself_alone(state):
    _runs_preload(state, _run_row(DC, GROUP_D3, "Warm Up"))
    _session(state, RACE_15, "15")
    _started(state, DD, RACE_15)
    assert state._class_code_scope()[1] == frozenset({"0x400035dd"})


def test_a_nameless_run_gives_its_group_but_is_never_picked_by_name(state):
    _runs_preload(state, _run_row(DC, GROUP_D3, "Warm Up"), _run_row(DD, GROUP_D3, ""))
    assert "" not in state.class_code_preload["runs_by_name"]
    _session(state, RACE_15, "15")
    assert state._class_code_scope() is None
    _started(state, DD, RACE_15)
    assert state._class_code_scope()[1] == frozenset({"0x400035dc", "0x400035dd", "0x800009d3"})


def test_class_code_records_survive_past_twelve_hours_but_not_past_thirty_six(state, monkeypatch):
    """A two-day meeting's grids are often built on day 1 and raced on day 2,
    so records are kept 36 h; but an unscoped read, which nothing keeps from
    another meeting's records, reads only a meeting day's."""
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _car_in_class(state, "8", "7654321", "Saloon Cup")
    _preload(state, _pre("9999999", "Unused Class", "UC"))
    _codes(state, "0x4000AAAA", _code_entry("e7", "7", "Saloon Cup", "SC", "1234567"))
    _entry_list(state, "0x4000BBBB", _code_entry("a0000008", "8", "Saloon Cup", "SC", "7654321"))
    snap = state.snapshot()
    assert _entry_for(snap, "7")["class_code"] == "SC"
    assert _entry_for(snap, "8")["class_code"] == "SC"
    now[0] += rs._CLASS_CODE_TTL_SECONDS + 1
    snap = state.snapshot()
    assert len(state.class_codes) == 2
    assert _entry_for(snap, "7")["class_code"] == ""
    assert _entry_for(snap, "8")["class_code"] == ""
    # The preload keeps its own, shorter life.
    assert state.class_code_preload["received_at"] is None
    now[0] = 1_000_000.0 + rs._PUSHED_CODE_TTL_SECONDS + 1
    state.prune_expired_class_codes()
    assert state.class_codes == {}


def test_an_unknown_run_falls_back_to_unscoped_and_reports_it(state, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="server.race_state")
    _runs_preload(state, _run_row())
    _session(state, "Mystery Session")
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _codes(state, "0x40001234", _code_entry("e7", "7", "Saloon Cup", "SC", "1234567"))
    snap = state.snapshot()
    assert snap["class_code_scope"] == ""
    assert _entry_for(snap, "7")["class_code"] == "SC"
    assert "scoped to no run (unscoped)" in caplog.text


def test_run_tags_match_regardless_of_hex_case(state):
    _session(state)
    _started(state, "0x400027FB")
    _car_in_class(state, "7", "1234567", "Saloon Cup")
    _codes(state, "0x400027fb", _code_entry("e7", "7", "Saloon Cup", "SC", "1234567"))
    snap = state.snapshot()
    assert snap["class_code_scope"] == "0x400027FB"
    assert _entry_for(snap, "7")["class_code"] == "SC"


def test_class_code_run_survives_init_and_round_trips(state):
    import json

    assert _started(state, age_seconds=30.0) == "class_codes"
    state.process({"type": "init"})
    assert state.class_code_run_next["run_id"] == "0x40002806"
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_run_next == state.class_code_run_next
    _session(restored)
    assert restored.class_code_run["session_number"] == "27"
    assert restored.snapshot()["class_code_scope"] == "0x40002806"


EVENT = "Scottish Championship Pre-Injection 600"


def _started_with_event(state, event=EVENT, run_id="0x40002806", start_key="k1"):
    return state.process({
        "type": "class_code_run", "run_id": run_id, "name": "Race 7 - 2nd Race",
        "start_key": start_key, "event": event, "age_seconds": 0.0,
    })


def test_class_code_run_keeps_an_optional_event(state):
    assert _started_with_event(state) == "class_codes"
    assert state.class_code_run_next["event"] == EVENT


def test_class_code_run_without_an_event_is_accepted(state):
    """An older relay sends no event."""
    assert _started(state) == "class_codes"
    assert "event" not in state.class_code_run_next


def test_a_non_string_event_is_ignored(state):
    assert _started_with_event(state, event=["x"]) == "class_codes"
    assert "event" not in state.class_code_run_next


@pytest.mark.parametrize("bound", [False, True])
def test_a_repeat_of_the_held_run_supplies_its_event(state, bound):
    """The relay's pick can precede the run's start notice, which then
    resends the run with its event; nothing else about the held run moves."""
    _started_with_event(state, event="")
    if bound:
        _session(state)
    held = state.class_code_run if bound else state.class_code_run_next
    before = dict(held)
    revision = state.class_codes_revision
    assert _started_with_event(state) == "class_codes"
    assert held["event"] == EVENT
    assert state.class_codes_revision == revision + 1
    assert {k: v for k, v in held.items() if k != "event"} == before
    # The same event again changes nothing.
    assert _started_with_event(state) is None


def test_a_restart_under_the_held_id_does_not_rename_the_held_start(state):
    """A stopped start stays bound, keeping its race name until ``$B,95``; a
    second start of the same id meanwhile must not rename it."""
    _started_with_event(state)
    _session(state)
    assert _started_with_event(state, event="Another Event", start_key="k2") is None
    assert state.snapshot()["race_name"] == EVENT


def test_class_code_run_event_survives_init_and_round_trips(state):
    import json

    _started_with_event(state)
    state.process({"type": "init"})
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_run_next["event"] == EVENT
    _session(restored)
    assert restored.snapshot()["race_name"] == EVENT


def test_race_name_is_shown_for_the_bound_run(state):
    _started_with_event(state)
    _session(state)
    assert state.snapshot()["race_name"] == EVENT


def test_race_name_is_blank_after_the_closing_95(state):
    _started_with_event(state)
    _session(state)
    _closing(state)
    assert state.snapshot()["race_name"] == ""


def test_race_name_is_blank_when_the_run_name_differs_from_the_session(state):
    _started_with_event(state)
    _session(state, "Race 8 - Final", number="28")
    assert state.snapshot()["race_name"] == ""


def test_race_name_is_blank_after_init_until_b_repopulates(state):
    _started_with_event(state)
    _session(state)
    state.process({"type": "init"})
    assert state.snapshot()["race_name"] == ""
    _session(state)
    assert state.snapshot()["race_name"] == EVENT


def test_race_name_is_blank_without_an_event(state):
    _started(state)
    _session(state)
    assert state.snapshot()["race_name"] == ""


@pytest.mark.parametrize("msg", [
    {"run_id": "0x40002806", "name": ""},
    {"run_id": "Race 7", "name": "Race 7 - 2nd Race"},
    {"run_id": "0x80000000", "name": "Race 7 - 2nd Race"},
    {"run_id": "0x80000985", "name": "Race 7 - 2nd Race"},
    {"run_id": ["0x40002806"], "name": "Race 7 - 2nd Race"},
    {"run_id": "0x40002806", "name": {"text": "Race 7 - 2nd Race"}},
    {"name": "Race 7 - 2nd Race"},
    {"run_id": "0x40002806", "name": "Race 7 - 2nd Race", "age_seconds": 13 * 3600},
])
def test_a_malformed_class_code_run_is_ignored(state, msg):
    assert state.process({"type": "class_code_run", **msg}) is None
    assert state.class_code_run is None


def test_class_code_run_does_not_make_stale_race_state_look_fresh(state, monkeypatch):
    import server.race_state as rs

    stale = state.last_updated
    monkeypatch.setattr(rs.time, "time", lambda: stale + 3600)
    assert _started(state) == "class_codes"
    assert state.last_updated == stale


def test_the_tag_on_every_push_is_never_a_scope(state):
    _runs_preload(state, _run_row(group_id="0x80000000"))
    _session(state)
    _started(state)
    assert state._class_code_scope() == ("0x40002806", frozenset({"0x40002806"}))
    _car_in_class(state, "72", "5588219", "Classic K")
    _codes(state, "0x80000000", _code_entry("e72", "72", "Classic K", "CM", "5588219"))
    assert _entry_for(state.snapshot(), "72")["class_code"] == ""


def test_runs_without_a_group_are_not_scoped_together(state):
    """``0x80000000`` is no group: another ungrouped run, perhaps another
    meeting's, is not this run's sibling."""
    _runs_preload(
        state,
        _run_row(group_id="0x80000000"),
        _run_row("0x40002807", "0x80000000", "Race 9"),
    )
    _session(state)
    _started(state)
    assert state._class_code_scope() == ("0x40002806", frozenset({"0x40002806"}))
    _car_in_class(state, "72", "5588219", "Classic K")
    _codes(state, "0x40002807", _code_entry("e72", "72", "Classic K", "CM", "5588219"))
    assert _entry_for(state.snapshot(), "72")["class_code"] == ""


def test_a_session_change_discards_the_started_run_it_does_not_name(state):
    """Run names repeat: a later same-named session the relay never saw start
    must not inherit the earlier run's id."""
    _session(state)
    _started(state)
    _session(state)  # $B repeats within a session
    assert state.class_code_run is not None
    _session(state, "Race 8")
    assert state.class_code_run is None
    _session(state)
    assert state.snapshot()["class_code_scope"] == ""


def test_a_run_announced_before_its_session_is_kept(state):
    _session(state, "Race 6 - AMENDED GRID")
    _started(state)
    _session(state)
    assert state.snapshot()["class_code_scope"] == "0x40002806"


def test_an_expired_started_run_dirties_the_state(state, monkeypatch):
    """An idle page must drop the scope when the run expires."""
    import server.race_state as rs

    now = [1_000_000.0]
    monkeypatch.setattr(rs.time, "time", lambda: now[0])
    _session(state)
    _started(state)
    state.mark_clean()
    assert state.prune_expired_class_codes() is False
    now[0] += rs._CLASS_CODE_TTL_SECONDS + 1
    assert state.prune_expired_class_codes() is True
    assert state.dirty
    assert state.class_code_run is None
    assert state.snapshot()["class_code_scope"] == ""


def _closing(state, description="Race 7 - 2nd Race"):
    state.process({"type": "run", "unique_number": "95", "description": description})


def test_a_session_end_keeps_its_run_in_scope_until_the_next_session(state):
    """95 keeps the closing description and the board still shows that
    session, so its run stays in scope; the next session's $B then discards
    it, so a same-named session the relay never saw start cannot inherit it."""
    _session(state)
    _started(state)
    _closing(state)
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    _closing(state)  # repeated
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    _session(state)
    assert state.class_code_run is None
    assert state.snapshot()["class_code_scope"] == ""


def test_a_closed_run_survives_a_restart_still_closed(state):
    import json

    _session(state)
    _started(state)
    _closing(state)
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_run == state.class_code_run
    _session(restored)
    assert restored.class_code_run is None


def test_only_the_edge_into_a_session_end_closes_a_started_run(state):
    """95 repeats between sessions; a run announced meanwhile is kept into its
    session, as is one announced before another session's end."""
    _session(state)
    _closing(state)
    _started(state)
    _closing(state)  # repeated, not an edge
    _session(state)
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    other = RaceState()
    _session(other, "Race 6 - AMENDED GRID")
    _started(other)
    _closing(other, "Race 6 - AMENDED GRID")
    _session(other)
    assert other.snapshot()["class_code_scope"] == "0x40002806"


def test_a_run_table_row_with_its_ids_in_the_wrong_form_is_skipped(state):
    _runs_preload(state, _run_row("0x80000985", "0x40002806"), _run_row("0x40002807"))
    assert [r["run_id"] for r in state.class_code_preload["runs"]] == ["0x40002807"]


def test_a_same_named_session_under_a_new_number_discards_the_old_run(state):
    """A boundary is the number changing, with or without a 95 between."""
    _session(state)
    _started(state)
    _session(state)  # repeated: binds the run to 27
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    _session(state, number="28")
    assert state.class_code_run is None
    assert state.snapshot()["class_code_scope"] == ""


def test_a_run_announced_just_before_its_same_named_session_is_kept(state):
    import json

    _session(state)
    _started(state)
    _session(state)
    _started(state, "0x40002807")  # the next session starts, then its $B
    _session(state, number="28")
    assert state.snapshot()["class_code_scope"] == "0x40002807"
    # Bound to 28, across a restart as well.
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_run == state.class_code_run
    restored.process({"type": "run", "unique_number": "29", "description": "Race 7 - 2nd Race"})
    assert restored.class_code_run is None


def test_a_same_named_run_starting_before_the_last_session_ends_waits_for_its_own(state):
    """The next run's start can arrive before the shown session's 95; it must
    not be scoped onto the old board, nor be closed by that 95."""
    _session(state)
    _started(state)
    _session(state)  # binds 0x40002806 to 27
    _started(state, "0x40002807")
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    _closing(state)
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    _session(state, number="28")
    assert state.snapshot()["class_code_scope"] == "0x40002807"
    assert state.class_code_run["session_number"] == "28"
    assert state.class_code_run_next is None


def test_a_repeated_class_code_run_keeps_its_session_binding(state):
    """The relay retries a delivery whose answer it lost."""
    _session(state)
    _started(state)
    _session(state)
    assert _started(state) is None
    assert state.class_code_run["session_number"] == "27"
    assert state.class_code_run_next is None
    _session(state, number="28")
    assert state.snapshot()["class_code_scope"] == ""


def test_a_closed_run_announced_again_is_a_restart(state):
    _session(state)
    _started(state)
    _session(state)
    _closing(state)
    assert _started(state) == "class_codes"
    _session(state, number="28")
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    assert state.class_code_run["session_number"] == "28"


def test_a_waiting_run_is_discarded_when_a_session_it_does_not_name_begins(state):
    _session(state, "Race 6 - AMENDED GRID", number="26")
    _started(state)  # waiting for "Race 7 - 2nd Race"
    _session(state, "Race 6 - AMENDED GRID", number="26")  # repeated
    _closing(state, "Race 6 - AMENDED GRID")
    assert state.class_code_run_next is not None
    _session(state, "Race 8", number="28")
    assert state.class_code_run_next is None
    _session(state, number="29")
    assert state.snapshot()["class_code_scope"] == ""


@pytest.mark.parametrize("data", ["nonsense", [], None, 42])
def test_load_class_codes_tolerates_a_store_that_is_not_a_mapping(state, data):
    state.load_class_codes(data)
    assert state.class_codes == {}
    assert state.class_code_run is None and state.class_code_run_next is None


def test_a_run_started_after_a_cold_session_ends_waits_for_its_own(state):
    """With nothing bound, a start arriving once the shown session has closed
    is the next session's, even under the same name."""
    _session(state)
    _closing(state)
    _started(state)
    assert state.snapshot()["class_code_scope"] == ""
    _session(state, number="28")
    assert state.snapshot()["class_code_scope"] == "0x40002806"


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------

def _announce(state, *rows, run_id="0x40002806"):
    return state.process({"type": "announcements", "run_id": run_id, "rows": [
        {"text": text, "ticks": ticks, "priority": "0"} for text, ticks in rows
    ]})


def _shown(state):
    return [a["text"] for a in state.snapshot()["announcements"]]


def test_a_run_announced_after_its_session_shows_then_binds_on_the_next_repeat(state):
    """A relay started mid-run picks the run by name and sends it after the
    session's ``$B``; the server needs nothing more to show and bind it."""
    _session(state, number="5")
    _started(state)
    assert state.class_code_run is None
    assert state.class_code_run_next["run_id"] == "0x40002806"
    _announce(state, ("Track clear", 100))
    assert _shown(state) == ["Track clear"]
    _session(state, number="5")
    assert state.class_code_run["run_id"] == "0x40002806"
    assert state.class_code_run["session_number"] == "5"
    assert state.snapshot()["class_code_scope"] == "0x40002806"
    assert _shown(state) == ["Track clear"]


def test_a_dropped_pick_accepted_late_resurfaces_no_rows_in_a_later_same_named_session(state):
    """The relay drops a pick on a session it does not name, but its start
    may still land afterwards; the clear the relay sends behind it leaves a
    later session of the same name nothing to show."""
    _session(state, number="5")
    _announce(state, ("Old notice", 100))
    _session(state, "Race 8", number="6")
    # The pick's delivery, accepted after the drop.
    state.process({
        "type": "class_code_run", "run_id": "0x40002806", "name": "Race 7 - 2nd Race",
        "event": "Old Championship", "age_seconds": 0.0,
    })
    state.process({
        "type": "announcements", "run_id": "0x40002806", "rows": [], "stopped": True,
        "dropped": True,
    })
    _session(state, number="7")
    assert _shown(state) == []
    assert state.snapshot()["race_name"] == ""


def test_announcements_show_while_their_run_is_the_running_session(state):
    _started(state)
    _session(state)
    assert _announce(state, ("Track clear", 100)) == "announcements"
    assert state.snapshot()["announcements"] == [{"key": "100", "text": "Track clear"}]


def test_announcements_stay_shown_through_the_95_close_edge(state):
    """Race control posts the reason for a stop after it, and the board
    still shows the closed session; the race name still hides there."""
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    _closing(state)
    assert _shown(state) == ["Track clear"]
    assert state.snapshot()["race_name"] == ""


def test_an_older_relays_stop_clear_still_empties_the_run(state):
    """A relay from before #107 sent empty rows on every stop; the server
    still applies them as the run's whole truth."""
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    assert _announce(state) == "announcements"
    assert _shown(state) == []


def test_announcements_of_another_run_are_not_shown(state):
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    _session(state, "Race 8 - Final", number="28")
    assert _shown(state) == []


def test_announcements_survive_init_and_reappear_with_the_sessions_run_record(state):
    """``$I`` is not a session signal: the rows are kept, hidden while the
    description is empty, and shown again once ``$B`` names the session."""
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    state.process({"type": "init"})
    assert state.announcements["0x40002806"]
    assert _shown(state) == []
    _session(state)
    assert _shown(state) == ["Track clear"]


def test_announcements_are_ordered_oldest_first_by_creation_ticks_not_priority(state):
    _started(state)
    _session(state)
    state.process({"type": "announcements", "run_id": "0x40002806", "rows": [
        {"text": "Newest", "ticks": 300, "priority": "0"},
        {"text": "Oldest", "ticks": 100, "priority": "9"},
        {"text": "Middle", "ticks": 200, "priority": "1"},
    ]})
    assert _shown(state) == ["Oldest", "Middle", "Newest"]
    assert all(set(a) == {"key", "text"} for a in state.snapshot()["announcements"])


def test_an_unchanged_announcements_message_does_not_dirty_the_state(state):
    _announce(state, ("Track clear", 100))
    state.mark_clean()
    assert _announce(state, ("Track clear", 100)) is None
    assert not state.dirty


def test_announcements_do_not_refresh_last_updated(state, monkeypatch):
    import server.race_state as rs

    stale = state.last_updated
    monkeypatch.setattr(rs.time, "time", lambda: stale + 3600)
    assert _announce(state, ("Track clear", 100)) == "announcements"
    assert state.last_updated == stale


@pytest.mark.parametrize("msg", [
    {"run_id": "0x40002806"},
    {"run_id": "0x40002806", "rows": "Track clear"},
    {"run_id": "Race 7", "rows": []},
    {"run_id": ["0x40002806"], "rows": []},
    {"rows": [{"text": "Track clear", "ticks": 1}]},
])
def test_malformed_announcements_messages_are_ignored(state, msg):
    assert state.process({"type": "announcements", **msg}) is None
    assert state.announcements == {}


def test_malformed_announcement_rows_are_dropped_or_coerced(state):
    state.process({"type": "announcements", "run_id": "0x40002806", "rows": [
        "Track clear", {"text": ""}, {"ticks": 5}, {"text": 7},
        {"text": "Kept", "ticks": "soon", "priority": 3},
    ]})
    assert state.announcements["0x40002806"] == [{"text": "Kept", "ticks": 0, "priority": ""}]


@pytest.mark.parametrize("closed_between", [False, True])
def test_announcements_do_not_carry_into_a_same_named_session_through_the_preload(
    state, closed_between
):
    """The preload's run table still names the old run, and the scope falls
    back to it by name; the announcements must not, or a same-named next
    session the relay never saw start would show the old run's rows."""
    _runs_preload(state, _run_row())
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    assert _shown(state) == ["Track clear"]
    if closed_between:
        _closing(state)
    _session(state, number="28")
    snap = state.snapshot()
    assert snap["class_code_scope"] == "0x40002806"  # the fallback still picks it
    assert snap["announcements"] == []


def test_another_runs_stop_does_not_clear_the_announcements_held(state):
    """An older relay cleared on every stop, including runs it never subscribed."""
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    assert _announce(state, run_id="0x40002805") is None
    assert _shown(state) == ["Track clear"]


def _announce_start(state, start_key, *rows, run_id="0x40002806"):
    return state.process({
        "type": "announcements", "run_id": run_id, "start_key": start_key,
        "name": "Race 7 - 2nd Race",
        "rows": [{"text": text, "ticks": ticks, "priority": "0"} for text, ticks in rows],
    })


RESTART_ID = "0x40002807"
RESTART_NAME = "Race 7 - 2nd Race - Re-Start"


def test_announcements_stay_until_the_next_sessions_b(state):
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    _closing(state)
    assert _shown(state) == ["Track clear"]
    _session(state, "Race 8 - Final", number="28")
    assert _shown(state) == []


def test_a_same_run_restart_shows_its_rows_again(state):
    """A red flag before a lap is completed: race control re-runs the same
    run, under a new start."""
    _started_with_event(state, start_key="k1")
    _session(state)
    _announce_start(state, "k1", ("Red flag", 100))
    _closing(state)
    assert _shown(state) == ["Red flag"]
    _started_with_event(state, start_key="k2")
    _session(state, number="28")
    _announce_start(state, "k2", ("Red flag", 100))
    assert state.class_code_run["start_key"] == "k2"
    assert _shown(state) == ["Red flag"]


def _restart(state, b_first):
    _started(state)
    _session(state)
    _announce(state, ("Red flag", 100))
    _closing(state)
    if b_first:
        _session(state, RESTART_NAME, number="28")
        _started(state, RESTART_ID, RESTART_NAME)
    else:
        _started(state, RESTART_ID, RESTART_NAME)
        _session(state, RESTART_NAME, number="28")
    _announce(state, ("Restart over 5 laps", 200), run_id=RESTART_ID)


@pytest.mark.parametrize("b_first", [True, False])
def test_a_restart_run_shows_the_previous_runs_rows_first(state, b_first):
    """A red flag after laps were run: race control restarts the race as a
    new run named the old one's name plus " - "."""
    _restart(state, b_first)
    assert _shown(state) == ["Red flag", "Restart over 5 laps"]


@pytest.mark.parametrize("b_first", [True, False])
def test_a_restart_runs_carry_ends_at_the_next_session(state, b_first):
    _restart(state, b_first)
    _session(state, "Race 8 - Final", number="29")
    assert _shown(state) == []
    _started(state, "0x40002808", "Race 8 - Final")
    assert _shown(state) == []


@pytest.mark.parametrize("restart_group, carried", [
    ("0x80000999", False),
    (None, True),
    ("0x80000985", True),
])
def test_a_run_of_a_different_group_does_not_carry(state, restart_group, carried):
    """A restart run created after the relay's pull has no known group, so
    only the name decides then."""
    runs = [_run_row()]
    if restart_group is not None:
        runs.append(_run_row(RESTART_ID, restart_group, RESTART_NAME))
    _runs_preload(state, *runs)
    _restart(state, b_first=False)
    assert _shown(state) == (
        ["Red flag", "Restart over 5 laps"] if carried else ["Restart over 5 laps"]
    )


def test_a_waiting_run_shown_for_its_session_carries_into_a_restart(state):
    """Its $B came before its start, and the restart's $B follows with no
    repeat and no 95 between, so it never became the bound run."""
    _session(state)
    _started(state)
    _announce(state, ("Red flag", 100))
    assert _shown(state) == ["Red flag"]
    _session(state, RESTART_NAME, number="28")
    _started(state, RESTART_ID, RESTART_NAME)
    _announce(state, ("Restart over 5 laps", 200), run_id=RESTART_ID)
    assert _shown(state) == ["Red flag", "Restart over 5 laps"]


def test_a_restart_runs_carry_survives_its_b_repeats_and_an_init(state):
    _restart(state, b_first=True)
    state.process({"type": "init"})
    _session(state, RESTART_NAME, number="28")
    _session(state, RESTART_NAME, number="28")
    assert _shown(state) == ["Red flag", "Restart over 5 laps"]


def test_a_restart_does_not_carry_past_a_session_with_no_run(state):
    """The carry is of the board's previous session only, so a session
    between with no run bound ends it."""
    _started(state)
    _session(state)
    _announce(state, ("Red flag", 100))
    _closing(state)
    _session(state, "Race 8 - Final", number="28")
    _closing(state, "Race 8 - Final")
    _session(state, RESTART_NAME, number="29")
    _started(state, RESTART_ID, RESTART_NAME)
    _announce(state, ("Restart over 5 laps", 200), run_id=RESTART_ID)
    assert _shown(state) == ["Restart over 5 laps"]


def test_late_rows_of_the_carried_run_still_show(state):
    """Race control posts after a stop, and a reply can arrive after the
    restart's $B has retired the old start."""
    _restart(state, b_first=True)
    _announce(state, ("Red flag", 100), ("Restart on the original grid", 150))
    assert _shown(state) == [
        "Red flag", "Restart on the original grid", "Restart over 5 laps",
    ]
    # Rows only: the retired start is not taken back.
    held = (state.class_code_run, state.class_code_run_next)
    assert [r["run_id"] for r in held if r is not None] == [RESTART_ID]


def test_a_dropped_clear_of_the_carried_run_keeps_its_rows(state):
    """A relay picking by name drops the old run when the restart's $B
    names no run it has pulled — after the server retired that start."""
    _restart(state, b_first=True)
    state.process({
        "type": "announcements", "run_id": "0x40002806", "rows": [],
        "stopped": True, "dropped": True,
    })
    assert _shown(state) == ["Red flag", "Restart over 5 laps"]


def test_a_late_reply_of_a_same_run_restarts_old_start_changes_nothing(state):
    _started_with_event(state, start_key="k1")
    _session(state)
    _announce_start(state, "k1", ("Red flag", 100))
    _closing(state)
    _started_with_event(state, start_key="k2")
    _session(state, number="28")
    _announce_start(state, "k2", ("Restart over 5 laps", 200))
    assert _announce_start(state, "k1", ("Red flag", 100)) is None
    assert _shown(state) == ["Restart over 5 laps"]


def test_a_run_whose_name_only_shares_a_prefix_does_not_carry(state):
    _started(state)
    _session(state)
    _announce(state, ("Red flag", 100))
    _closing(state)
    _started(state, RESTART_ID, "Race 7 - 2nd Race Final")
    _session(state, "Race 7 - 2nd Race Final", number="28")
    _announce(state, ("Final notice", 200), run_id=RESTART_ID)
    assert _shown(state) == ["Final notice"]


def test_a_refreshed_session_keeps_its_announcements_past_the_run_ttl(state, monkeypatch):
    """The relay's refreshes prove the run is still started, so a session
    running past the 12-hour run TTL keeps its announcements."""
    import server.race_state as rs

    t0 = time.time()
    monkeypatch.setattr(rs.time, "time", lambda: t0)
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    for hours in (2, 4, 6, 8, 10, 12, 14):
        monkeypatch.setattr(rs.time, "time", lambda h=hours: t0 + h * 3600)
        _announce(state, ("Track clear", 100))
    assert _shown(state) == ["Track clear"]


def test_a_closed_run_refreshed_overnight_keeps_its_announcements(state, monkeypatch):
    """The announcements show until the next session, however long the gap,
    while the relay keeps refreshing the stopped run."""
    import server.race_state as rs

    t0 = time.time()
    monkeypatch.setattr(rs.time, "time", lambda: t0)
    _started(state)
    _session(state)
    _announce(state, ("Track clear", 100))
    _closing(state)
    for hours in (2, 4, 6, 8, 10, 12, 14):
        monkeypatch.setattr(rs.time, "time", lambda h=hours: t0 + h * 3600)
        _announce(state, ("Track clear", 100))
        state.prune_expired_class_codes()
    assert _shown(state) == ["Track clear"]


def test_a_stop_clear_does_not_renew_its_run(state, monkeypatch):
    import server.race_state as rs

    t0 = time.time()
    monkeypatch.setattr(rs.time, "time", lambda: t0)
    _started(state)
    _session(state)
    monkeypatch.setattr(rs.time, "time", lambda: t0 + 2 * 3600)
    state.process({"type": "announcements", "run_id": "0x40002806", "rows": [], "stopped": True})
    assert state.class_code_run["received_at"] == t0


def _refresh(state, *rows, name="Race 7 - 2nd Race"):
    return state.process({"type": "announcements", "run_id": "0x40002806", "name": name, "rows": [
        {"text": text, "ticks": ticks, "priority": "0"} for text, ticks in rows
    ]})


def test_a_server_that_lost_the_started_run_restores_it_from_a_refresh(state):
    """A crash after the start was accepted but before it was saved: the relay
    never sends the start again, so its refresh restores the binding."""
    _session(state)
    _refresh(state, ("Track clear", 100))
    assert state.class_code_run["run_id"] == "0x40002806"
    assert _shown(state) == ["Track clear"]


def test_a_refresh_restores_the_race_name_with_the_started_run(state):
    _session(state)
    state.process({
        "type": "announcements", "run_id": "0x40002806", "name": "Race 7 - 2nd Race",
        "start_key": "k1", "event": EVENT, "rows": [],
    })
    assert state.class_code_run["event"] == EVENT
    assert state.snapshot()["race_name"] == EVENT


def test_a_refresh_supplies_the_event_of_the_held_start_only(state):
    """A run held without an event — from an older relay, or restored before
    the relay knew one — takes it from a refresh of the same start."""
    _started_with_event(state, event="")
    _session(state)
    refresh = {
        "type": "announcements", "run_id": "0x40002806", "name": "Race 7 - 2nd Race",
        "start_key": "other", "event": EVENT, "rows": [],
    }
    state.process(refresh)
    assert state.snapshot()["race_name"] == ""
    state.process(refresh | {"start_key": "k1"})
    assert state.snapshot()["race_name"] == EVENT


def test_a_stop_of_the_bound_run_keeps_its_race_name_until_the_95(state):
    """Only a waiting start loses its event to a stop; the board still shows
    the session that ran."""
    _started_with_event(state)
    _session(state)
    state.process({
        "type": "announcements", "run_id": "0x40002806", "rows": [], "stopped": True,
        "start_key": "k1",
    })
    assert state.snapshot()["race_name"] == EVENT
    _closing(state)
    assert state.snapshot()["race_name"] == ""


def test_a_stop_of_the_waiting_run_on_show_keeps_its_race_name(state):
    """``$B`` can come before a mid-run pick, which then waits — shown — until
    a ``$B`` repeat binds it; a stop meanwhile is not a dropped pick."""
    _session(state)
    _started_with_event(state)
    assert state.class_code_run is None
    state.process({
        "type": "announcements", "run_id": "0x40002806", "rows": [], "stopped": True,
        "start_key": "k1",
    })
    assert state.snapshot()["race_name"] == EVENT


def test_a_stop_between_init_and_its_b_keeps_the_waiting_runs_race_name(state):
    """``$I`` blanks the description, which is no sign the waiting run was
    dropped; the same session's ``$B`` then binds it with its name."""
    _session(state)
    _started_with_event(state)
    state.process({"type": "init"})
    state.process({
        "type": "announcements", "run_id": "0x40002806", "rows": [], "stopped": True,
        "start_key": "k1",
    })
    _session(state)
    assert state.snapshot()["race_name"] == EVENT


def test_an_event_correction_after_the_95_does_not_revive_the_closed_start(state):
    _started_with_event(state, event="")
    _session(state)
    _closing(state)
    assert _started_with_event(state) is None
    assert state.class_code_run_next is None
    _session(state, number="28")
    assert state.snapshot()["race_name"] == ""


def test_a_restart_after_the_95_under_a_new_start_key_is_taken(state):
    _started_with_event(state)
    _session(state)
    _closing(state)
    assert _started_with_event(state, start_key="k2") == "class_codes"
    assert state.class_code_run_next["start_key"] == "k2"


def _dropped(state, start_key="k1"):
    state.process({
        "type": "announcements", "run_id": "0x40002806", "rows": [], "stopped": True,
        "dropped": True, "start_key": start_key,
    })


def test_a_genuine_stop_while_another_session_runs_keeps_the_race_name(state):
    """The next run can start and stop before its own ``$B``; only a dropped
    pick's clear drops a race name, never the description disagreeing."""
    _session(state, "Race 6", number="26")
    _started_with_event(state)
    state.process({
        "type": "announcements", "run_id": "0x40002806", "rows": [], "stopped": True,
        "start_key": "k1",
    })
    _session(state)
    assert state.snapshot()["race_name"] == EVENT


def test_a_dropped_picks_clear_after_init_drops_its_race_name(state):
    """``$I`` blanks the description; the dropped mark alone decides."""
    _session(state, number="5")
    _session(state, "Race 8", number="6")
    _started_with_event(state)  # the pick's delivery, accepted after the drop
    state.process({"type": "init"})
    _dropped(state)
    _session(state, "Race 8", number="6")
    _session(state, number="7")
    assert state.snapshot()["race_name"] == ""


def test_a_stop_of_another_start_leaves_the_waiting_runs_event(state):
    _started_with_event(state)
    _dropped(state, start_key="retired")
    assert state.class_code_run_next["event"] == EVENT


def test_a_refresh_does_not_restore_a_run_discarded_at_a_session_boundary(state):
    _started(state)
    _session(state)
    _refresh(state, ("Track clear", 100))
    _session(state, number="28")
    _refresh(state, ("Track clear", 100))
    assert state.class_code_run is None
    assert _shown(state) == []


def test_a_refresh_restores_nothing_for_another_session(state):
    _session(state, "Race 8 - Final", number="28")
    _refresh(state, ("Track clear", 100))
    assert state.class_code_run is None


def test_a_refresh_after_the_95_restores_the_closed_sessions_run_closed(state):
    """The relay keeps a stopped run subscribed, so a start lost until after
    the close is restored as the 95 would have left it: its rows shown, its
    race name hidden, and the next session's $B discarding it."""
    _session(state)
    _closing(state)
    _refresh(state, ("Track clear", 100))
    assert state.class_code_run["closed"]
    assert _shown(state) == ["Track clear"]
    assert state.snapshot()["race_name"] == ""
    _session(state, "Race 8 - Final", number="28")
    assert state.class_code_run is None
    assert _shown(state) == []


def test_a_previous_runs_store_does_not_block_restoring_the_current_run(state):
    """The crash lost the new run's start, and the store still holds the
    previous run; the next $B discards that, and the refresh restores the
    current one."""
    import json

    _started(state, run_id="0x40002805", name="Race 6")
    _session(state, "Race 6", number="26")
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_run["run_id"] == "0x40002805"
    _session(restored)
    assert restored.class_code_run is None
    _refresh(restored, ("Track clear", 100))
    assert restored.class_code_run["run_id"] == "0x40002806"
    assert _shown(restored) == ["Track clear"]


def test_a_retired_run_stays_retired_across_a_restart(state):
    """A same-named next session discarded the run; after a reload, the old
    subscription's refresh must not restore it."""
    import json

    _started(state)
    _session(state)
    _session(state, number="28")
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    _session(restored, number="28")
    _refresh(restored, ("Track clear", 100))
    assert restored.class_code_run is None
    assert _shown(restored) == []


def test_retired_runs_expire_with_the_run_ttl():
    restored = RaceState()
    now = time.time()
    restored.load_class_codes({"retired_runs": {
        "0x40002806\tk1": now - 13 * 3600, "0x40002805\tk2": now - 3600,
        "Race 7\tk3": now, "0x40002804\tk4": "soon", "0x40002803": now,
    }})
    assert restored._retired_runs == {"0x40002805\tk2": now - 3600}


def test_the_next_runs_rows_do_not_hide_the_current_runs_before_the_boundary(state):
    """The next run can start, and its rows arrive, before the current session ends."""
    _started(state, run_id="0x40002805", name="Race 6")
    _session(state, "Race 6", number="26")
    _announce(state, ("Current", 100), run_id="0x40002805")
    _started(state)
    _announce(state, ("Next", 200))
    assert _shown(state) == ["Current"]
    _session(state)
    assert _shown(state) == ["Next"]


def test_a_run_restarted_under_a_retired_id_is_restored(state):
    """A restart under the same id is a new start with its own key: its
    refresh restores it, while the retired start's never does — however
    late a retry of it arrives."""
    import json

    state.process({"type": "class_code_run", "run_id": "0x40002806",
                   "name": "Race 7 - 2nd Race", "start_key": "first"})
    _session(state)
    _session(state, "Race 8 - Final", number="28")
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    _session(restored, number="29")
    old = {"type": "announcements", "run_id": "0x40002806", "name": "Race 7 - 2nd Race",
           "rows": [{"text": "Track clear", "ticks": 1}], "start_key": "first"}
    restored.process(old)
    assert restored.class_code_run is None
    restored.process(old | {"start_key": "second"})
    assert restored.class_code_run["run_id"] == "0x40002806"
    assert _shown(restored) == ["Track clear"]


def test_a_restart_lost_in_a_crash_is_restored_after_its_saved_closed_run_retires(state):
    """The store holds the run's earlier, closed start; the restart was lost.
    The next $B retires the saved start only, so the restart's refresh restores."""
    import json

    state.process({"type": "class_code_run", "run_id": "0x40002806",
                   "name": "Race 7 - 2nd Race", "start_key": "first"})
    _session(state)
    _closing(state)
    restored = RaceState()
    restored.load_class_codes(json.loads(json.dumps(state.class_codes_to_dict())))
    assert restored.class_code_run["closed"]
    _session(restored, number="28")
    assert restored.class_code_run is None
    restored.process({"type": "announcements", "run_id": "0x40002806",
                      "name": "Race 7 - 2nd Race", "start_key": "second",
                      "rows": [{"text": "Track clear", "ticks": 1}]})
    assert restored.class_code_run["start_key"] == "second"
    assert _shown(restored) == ["Track clear"]