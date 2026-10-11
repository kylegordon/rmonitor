---
name: rmonitor-display
description: How the timing pages and the server agree on what to show. Use when touching the web pages (`/` and `/tv`), the WebSocket payload, sort order, the Gap/Diff or time columns, page versioning, or how server/server.py serves the page.
paths:
  - server/server.py
  - "server/templates/**"
  - server/race_state.py
---

# Timing page and payload

Rules that keep the timing page and the server's payload agreeing. How an interval is
measured stays in `server/AGENTS.md`.

## A long-open tab can run an old page against a new server

The page and the server are two halves of one behaviour, and a long-open tab can run an
old half against a new one with its data still live — nothing looks broken, so nothing
reports it. Hence `handle_index` serves `Cache-Control: no-cache` plus an `ETag` derived
from the template's content, and every WebSocket payload carries `page_version`; a mismatch
**prompts** and never self-reloads, because these displays are on users' own devices. The
older `server_instance_id` guard tracks the process, not the page, and *does* self-reload,
so it is checked second and yields — a deploy changes both. Guarded by the `test_index_*`
tests and `test_ws_full_message_carries_the_page_version` in `tests/test_server.py`, and
`test_the_page_version_token_is_substituted_by_the_server` and
`test_an_outdated_page_prompts_rather_than_reloading_itself` in
`tests/test_repo_invariants.py`.

## One signal drives both the sort order and the interval reference

`RaceState._sort_mode()` returns the single value that picks the sort key *and* the
interval branch, keyed on the `session_mode` label — never directly on `_is_qualifying`,
which any `$G` clears permanently and which measures `False` in every capture here. The
flag still reaches the sort through the label: `_derive_session_mode` tests it first and
unconditionally, so it overrides `$B` until a session's first `$G`. `Gap` means "interval
to the row above", so values and order that disagree are worse than either alone. Both
guarded in `tests/test_race_state.py`, by
`test_practice_session_sorts_by_best_lap_and_derives_intervals_from_them` and
`test_early_qual_info_overrides_a_race_description_until_the_first_race_info`.

## A negative `Gap` is blanked, never rendered and never converted to `+1 L`

The sign does not identify a stalled car — an ordinary pair inside the one-lap crossing
window goes negative too — so `+1 L` there lands on cars seconds apart and flickers once
a lap. Blank it; `_apply_intervals`' docstring carries the arithmetic. Guarded by
`test_negative_gap_is_blanked` and
`test_negative_gap_in_the_crossing_window_is_not_a_lap_deficit` in `tests/test_race_state.py`.

## `sort_mode` in the payload is what the page reads

`snapshot()` states which order it sorted in; `server/templates/index.html` reads that
field, never re-deriving it from `session_mode` and `flag`, and so do its time columns
(Total Time under a position sort). Guarded by `test_snapshot_reports_the_sort_mode_it_used`,
`test_the_page_reads_sort_mode_and_does_not_re_derive_it` and
`test_the_time_columns_follow_sort_mode_not_session_mode`. What the page *draws* — row
index, tooltip, arrows, which time column shows — is **unguarded**; check it by eye.

## The fastest-lap row is the server's call

`snapshot()` sets each entry's `fastest_lap` from Orbits' `$H` best-lap rank, kept as
`best_lap_rank` even in a race (where `position` must not move); the lowest valid best lap
only breaks the handover double claim and drops no-time rank-1 rows. The page just toggles
`tr.fastest-lap` (purple row, bold Best Lap where shown, "fastest lap" for screen
readers only) and never compares times. Guarded by
`test_fastest_lap_handover_double_claim_flags_only_the_faster_car` and its siblings in
`tests/test_race_state.py`, and `test_the_page_shades_the_fastest_lap_row_from_the_server_flag`.
What it draws is checked by eye.

## A status replaces Diff, and a DNF's Total Time reads RETIRED

Each snapshot entry carries `status` (DNF, DNS, DQ or `""`), joined from the run's
entry list as the class codes are and set only in a `Race` session not under the purple-flag
sort. It reads only the started run bound to the session, never the scope's name fallback, so
a same-named earlier race's statuses are never shown; with no such run it is blank. The page
shows whatever is set — the status in Diff, and RETIRED in Total Time for a DNF — without
re-checking the session. Rows are not re-sorted
or styled: the host already lists them after the classified cars. Guarded by
`test_status_is_blank_outside_a_race`, `test_status_is_blank_under_a_purple_flag` and
`test_conflicting_statuses_leave_the_car_blank` in `tests/test_race_state.py`, and
`test_the_page_shows_a_status_in_diff_and_retired_in_total` in `tests/test_repo_invariants.py`.

## The /tv page is an unattended broadcast overlay

`server/templates/tv.html` is its own template, served by `handle_tv` with its own ETag. It
reads the same `/ws` snapshot and draws `entries` in server order, with `sort_mode`,
`fastest_lap` and `status` as sent. No one is at the broadcast browser, so it ignores
`page_version` and self-reloads on a `server_instance_id` change; a deploy's brief blank on
air is accepted. It sets no page background (OBS and vMix key on the alpha), loads nothing
external, writes feed text through `textContent` only, and moves rows by `transform`. A
`full` reveals the tower only after a short wait that `no_feed` cancels, since a connect
during an outage gets a stale `full` first. `Finish` draws a chequered flag bar.
Guarded by the `test_tv_*` tests in `tests/test_server.py`, and
`test_the_tv_page_reloads_on_a_restart_and_never_reads_page_version`,
`test_the_tv_page_is_transparent_and_self_contained` and
`test_the_tv_tower_draws_server_values_as_text_and_moves_rows_by_transform`,
`test_the_tv_page_holds_a_stale_full_back_until_no_feed_is_ruled_out` and
`test_the_tv_page_shows_the_finish_flag_as_chequered` in
`tests/test_repo_invariants.py`. What it draws is checked by eye.
