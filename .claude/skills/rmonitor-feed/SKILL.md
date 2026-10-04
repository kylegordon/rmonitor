---
name: rmonitor-feed
description: Traps in the rMonitor feed and the race state built from it. Use when touching rMonitor parsing, RaceState, session mode, session boundaries, competitor keying or any protocol record ($A, $COMP, $B, $I, $F, $G, $SP, $SR) in server/race_state.py, relay/rmonitor_client.py or their tests.
paths:
  - server/race_state.py
  - relay/rmonitor_client.py
  - tests/test_parser.py
  - tests/test_race_state.py
  - "tests/fixtures/**"
---

# rMonitor feed and race state

What you need to know before changing how the feed is parsed or how `RaceState` folds it
into a session. Each section names the tests that guard it; the history of how each was
found lives in those tests' docstrings.

## `reg_number` is the key, `number` is display text

`reg_number` is the internal registration key (e.g. `"21"`); `number` is the displayed car
number, possibly with letters (`"12X"`). Always key competitor dicts by `reg_number`.
`RaceState._competitor` also skips empty incoming values rather than blanking a field an
earlier `$A`/`$COMP` filled. Guarded by
`test_competitor_keyed_by_reg_number_not_displayed_number` and
`test_competitor_name_not_blanked_by_empty_update` in `tests/test_race_state.py`.

## Session mode runs on two signals and only one of them is live

The `session_mode` label, derived by substring match on `run_description`, is the one that
matters; `_is_qualifying` is set by message type and cleared permanently by any `$G`, so it
measures `False` in every capture here — but `_derive_session_mode` tests it first and
unconditionally, so until a session's first `$G` it overrides the description outright.
Read `_derive_session_mode` and `_qual_info` together before touching either. What the label
then drives — the sort order and the interval reference — is in the `rmonitor-display`
skill and `server/AGENTS.md`. Guarded by
`test_qual_info_during_a_race_does_not_overwrite_race_positions` and the `session_mode` and
sort-order tests in `tests/test_race_state.py`.

## The rMonitor protocol is not fully documented

`$SP`/`$SR` appear in no spec but are real output from some Orbits setups, and the `$F`
flag is a **fixed-width six-character field** — `_tokenize` strips the padding, but a
longer name would truncate, so never compare against one over six characters. Blank is
ambiguous, covering pre-session, formation lap, between sessions and post-finish alike.
Trust `relay/rmonitor_client.py` over the spec. Guarded by `test_heartbeat_flag_trim`,
`test_heartbeat_flag_padding_is_stripped`, `test_captured_flag_fields_are_all_six_characters`
(which measures the width over the committed fixtures rather than a hard-coded list),
`test_lap_info_sp` and `test_lap_info_sr` in `tests/test_parser.py`.

## Session boundaries come from `$B`, and are edges — never `$I`

`$I` is emitted inconsistently — none on a scoreboard reset, one after a finished race,
three at a session start — and each is a live `RaceState.reset()` plus a broadcast, made
safe only by the repopulating records that follow in the same batch. `$B,95` closes the
running session, carrying its description; any other number names the session that is
current. Both recur, an active record up to 264 times, so a boundary is `unique_number`
*changing*, and a capture cut mid-session simply has no closing 95. Guarded by
`test_every_opened_session_is_closed_by_a_95_carrying_its_description` and
`test_captured_run_records_repeat_so_a_boundary_is_an_edge` in `tests/test_parser.py`,
`test_repeated_init_then_repopulate_leaves_state_correct` in `tests/test_race_state.py`, and
`test_repeated_init_wipes_reach_clients_before_repopulation` in `tests/test_server.py` for
the broadcast-per-init over the real ingest path.
