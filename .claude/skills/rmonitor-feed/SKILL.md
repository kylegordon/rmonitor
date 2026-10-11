---
name: rmonitor-feed
description: Traps in the rMonitor feed and the race state built from it, and how a time penalty shows on both ports. Use when touching rMonitor parsing, time penalties or corrections, the :51738 results view columns (including Orbits' own Gap and Diff), RaceState, session mode, session boundaries, competitor keying, a car's DNF/DNS/DQ status or any protocol record ($A, $COMP, $B, $I, $F, $G, $SP, $SR) or a deleted passing in server/race_state.py, relay/rmonitor_client.py, relay/class_code_client.py or their tests.
paths:
  - server/race_state.py
  - relay/rmonitor_client.py
  - relay/class_code_client.py
  - tests/test_parser.py
  - tests/test_race_state.py
  - tests/test_class_code_client.py
  - "captures/*.log"
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

## A deleted passing is a `$G` with an empty lap count

When race control deletes a crossing, Orbits re-sends the car's `$G` with no lap count and
`00:00:00.000`. `RaceState` then clears the car's laps and withdraws its times from
`lap_times`, which is kept per car for that reason. Guarded by
`test_a_deleted_passing_leaves_no_negative_interval` in `tests/test_race_state.py`.

## A time penalty is `TotalTimeCorrection`, not `HasPenalties`

An operator's manual time correction is added into the `$G` race time with no marker on
`:50000`. On `:51738` it shows in the results view as `TotalTimeCorrection` above zero on the
car's row, while `HasPenalties` stays 0. Nothing reads that column yet.

## Orbits sends Gap and Diff on `:51738`; the server derives its own from `:50000`

The rMonitor feed carries no intervals, so `RaceState` computes `Gap` and `Diff`. The results
view does carry them (columns 47, 48 and 53, with the car number behind in 54), as the Orbits
client shows them, but nothing reads them yet and they refresh only with the relay's 60 s poll.
All 87 columns, the frame layout and the cell encoding are in
[results-view.md](results-view.md).

## A car's DNF, DNS or DQ is only in the `:51738` results view, read from the row middle

No rMonitor record carries a per-car status. Each results-view row's middle holds it as a
`str` followed by ten zero bytes and a time `str` — `Active` (finishers too), DNF, DNS or
DQ — which `_entry_list_status` in `relay/class_code_client.py` reads; the `str` before the
row's head holds a position or a status and is not read. The relay sends every row's status in
`statuses` on the entry-list message, code or none. `RaceState` keeps every row in
`run_statuses` (Active ones make a join ambiguous) and shows only DNF/DNS/DQ — one set per run
that each list replaces whole, apart from the class-code records because a push overwrites those. Like them it survives `reset()`, expires on
`_PUSHED_CODE_TTL_SECONDS` and is saved with the class-code store. Guarded by
`test_results_excerpt_reads_real_statuses` and `test_entry_list_rows_read_each_rows_status` in
`tests/test_class_code_client.py`, and `test_a_push_does_not_clear_a_status` and
`test_a_newer_entry_list_withdraws_a_cleared_status` in `tests/test_race_state.py`.
