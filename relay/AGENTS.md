# relay/AGENTS.md — scoped rules for the relay

The repository-wide rules in the root `AGENTS.md` apply here in full. These add to them,
and are loaded only when an agent is working in this directory.

## The `:51738` session handle and unit id are read live, never replayed

`relay/class_code_client.py` reads both from the server's identity frame on every connect and
writes them into the later handshake records and the keepalive. The handle is per server and
changes from day to day. A stale one gets a 2-byte answer and no pushes, which is
indistinguishable from a broken protocol — so there is no fallback to a remembered or
placeholder value, and no identity frame means the connection is abandoned. Guarded by
`test_handshake_sends_five_records_with_the_live_handle_and_unit` and
`test_no_identity_frame_sends_only_record_0_then_backs_off` in
`tests/test_class_code_client.py`.

## Every `:51738` connect shows a notice on the timing operator's screen

So the client never treats silence as failure — pushes follow entry-list edits, and the
connection is quiet in between — and it reconnects only after a real failure, with a slow,
doubling backoff. A read timeout or a fast retry loop would flood the operator's screen.
Guarded by `test_silence_does_not_trigger_a_reconnect` and
`test_reconnect_delay_doubles_to_the_cap` in `tests/test_class_code_client.py`.

## Class codes are withheld, never guessed

The relay forwards the pushed fields raw and never derives a code from a class name: across
one season the mapping from names to codes is many-to-many. A record without a code is
dropped. The join to rMonitor competitors is not the relay's: its rules live in the
docstring of `RaceState._resolve_class_codes`, beside the tests that guard them. Guarded
here by `test_record_entry_skips_short_or_codeless_records` in
`tests/test_class_code_client.py`.

## The registry preload is trusted only under the class-uniform guard

Every connect also pulls the host's competitor registry and forwards it raw as one preload:
each registration's id, number, transponder, class and code, plus the host's run table. The
server matches a car to one registration by number and exact class, or by transponder and
class, and a conflict between the two blanks the cell. It then uses the registration's code
only when every registry record with that class name carries the same code, because a feed car
can still land on someone else's registration — a transponder that changed hands, a number
reused within a class — and a registry code may disagree with the meeting's pushes; pushes
always win, and a class whose pushes show another code is withheld. The guard reads the whole
registry, so a pull cut short by its time or size cap is never forwarded (completeness is
otherwise inferred from the stream going quiet). Guarded by
`test_preload_code_is_withheld_when_the_class_name_spans_several_codes` and
`test_preload_is_blank_when_transponder_and_number_name_different_registrations` in
`tests/test_race_state.py`, and
`test_an_incomplete_registry_pull_keeps_the_connection_and_sends_no_preload` in
`tests/test_class_code_client.py`.

## An announcements push is a change signal; only a subscription reply is the truth

The timing host pushes every create, edit and delete to each open announcements view, but a
delete push still carries the deleted row, so no push is read as the table. A push only makes
the client subscribe again on a new view, and that reply is forwarded. Guarded by
`test_a_delete_push_triggers_a_resubscribe_whose_reply_is_the_truth` and
`test_the_reply_to_a_resubscribe_does_not_trigger_another` in `tests/test_class_code_client.py`.

## The entry list rides the held connection, as a snapshot

The running run's results view lists every entrant with the code the timing host holds, so it
fills codes no push carried. It is subscribed only on the held `:51738` connection, never on one
of its own, and an unanswered subscription is retried on that same socket: every connect raises an
operator notice, so a per-run or per-retry connection would be a stream of them. Its reply is
taken once and the view closed, because an open results view streams the whole table about once a
second; a slow refresh catches mid-run edits. The code comes only from the host's own code field,
and a reply whose rows disagree with its count is withheld. Each list is the run's whole entry
list, so the server replaces the run's earlier list with it, withdrawing a code since cleared.
Guarded by `test_an_unanswered_entry_list_subscription_is_retried_on_the_same_connection`,
`test_a_started_run_subscribes_its_entry_list_and_forwards_the_rows` and
`test_entry_list_parser_withholds_a_reply_whose_row_count_disagrees` in
`tests/test_class_code_client.py`, and
`test_an_entry_list_withdraws_a_code_its_runs_earlier_list_gave` in `tests/test_race_state.py`.
