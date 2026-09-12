# server/AGENTS.md — scoped rules for the server

The repository-wide rules in the root `AGENTS.md` apply here in full. These add to them,
and are loaded only when an agent is working in this directory.

## aiohttp: never reassign `AppKey` values after app startup

`app[some_key] = new_value` in a request handler or background task triggers
`DeprecationWarning: Changing state of started or joined application is deprecated`. Store a
mutable container under the key at creation and mutate it:

```python
# At app creation (fine):
app[my_key] = {"value": None, "flag": False}
# In a handler (fine — mutating the dict, not the key):
app[my_key]["value"] = time.monotonic()
# BAD — reassigning the key after startup:
app[my_key] = time.monotonic()
```

## Stateful features: initialise timestamps at startup, never lazily

If a watchdog or timeout compares `time.monotonic() - last_seen`, initialise `last_seen` to
`time.monotonic()` in the app startup hook, not to `None` — an `if last is not None` guard never
fires on a fresh server that has never received data.

The same reasoning applies to a newly connected client: `handle_ws` must reflect the *current*
server state immediately, so if the feed is already known lost or timed out it sends `no_feed`
right after the initial `full` message. Otherwise the client shows stale persisted data until the
next watchdog poll.

## An interval between competitors is measured at each car's own lap

`Gap` and `Diff` are derived against the leader's time **at each competitor's own lap**,
via the `RaceState.leader_time_at_lap` index — never by subtracting two `total_time`
values out of one snapshot. Two cars are almost always on different lap counts, so that
subtraction reports a car that is behind as being ahead, and the sign flips every time
the leader crosses the line. Guarded by
`test_gap_is_positive_when_the_leader_is_a_lap_ahead` in `tests/test_race_state.py`.

The **read** side obeys the same `$G`-only rule the index's write side does: both halves
of both columns — the seconds and the `+N L` deficit — come from the
`(timed_lap, timed_lap_seconds)` pair stamped in `RaceState._race_info`, never from the
live `laps` and `total_time` fields. Those two are written by three handlers
independently and are not a pair, so reading them together makes the whole field flash
negative or blank for a lap. Guarded by
`test_no_negative_interval_in_the_window_between_a_passing_and_its_race_info` in
`tests/test_race_state.py`.

## One signal drives both the sort order and the interval reference

`RaceState._sort_mode()` returns the single value that picks the sort key *and* the
interval branch, and it keys on the `session_mode` label — never directly on
`_is_qualifying`, which any `$G` clears permanently and which therefore measures `False`
in every capture and example file here. The label usually comes from `$B`, but
`_derive_session_mode` falls back to `_is_qualifying` when there is no `$B` at all: that
pure-`$H` path is the one route by which the flag still reaches the sort, and it is
supported, not vestigial. `Gap` means "interval to the row above", so values and order
that disagree are worse than either alone. Guarded by
`test_practice_session_sorts_by_best_lap_and_derives_intervals_from_them` in
`tests/test_race_state.py`.

## A negative `Gap` is a stalled car, never a negative time

A car that has pitted or retired keeps a frozen deficit while the leader laps on, so
subtracting two deficits goes negative. The subtraction is this row's deficit minus the
row above's, so the stalled car is the **lower** row — the entry being written, not the
one above it. Report the lap difference (`+N L`) where one exists and leave the column
blank where it does not. Guarded by `test_negative_gap_becomes_a_lap_deficit` in
`tests/test_race_state.py`.

## `sort_mode` in the payload is what the page reads

`snapshot()` states which order it sorted in, and `templates/index.html` reads that field
rather than re-deriving the condition from `session_mode` and `flag` — nothing here
renders the template, so a duplicated condition is a rule no test can reach. Guarded by
`test_snapshot_reports_the_sort_mode_it_used` in `tests/test_race_state.py`.
