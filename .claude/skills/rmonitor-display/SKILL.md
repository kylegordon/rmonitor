---
name: rmonitor-display
description: How the timing page and the server agree on what to show. Use when touching the web page, the WebSocket payload, sort order, the Gap/Diff or time columns, page versioning, or how server/server.py serves the page.
paths:
  - server/server.py
  - "server/templates/**"
  - server/race_state.py
---

# Timing page and payload

The page and the server are two halves of one behaviour. These are the rules that keep
them agreeing; how an interval is measured stays in `server/AGENTS.md`.

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
