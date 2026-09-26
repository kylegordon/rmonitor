# relay/AGENTS.md — scoped rules for the relay

The repository-wide rules in the root `AGENTS.md` apply here in full. These add to them,
and are loaded only when an agent is working in this directory.

## The `:51738` session handle and unit id are read live, never replayed

`relay/class_code_client.py` reads both from the server's identity frame on every connect and
writes them into the later handshake records and the keepalive. The handle is per server and
changes from day to day. A stale one gets a 2-byte answer and no pushes, which is
indistinguishable from a broken protocol — so there is no fallback to a remembered or
placeholder value, and no identity frame means the connection is abandoned. Guarded by
`test_handshake_sends_three_records_with_the_live_handle_and_unit` and
`test_no_identity_frame_sends_only_record_0_then_backs_off` in
`tests/test_class_code_client.py`.

## Every `:51738` connect shows a notice on the timing operator's screen

So the client never treats silence as failure — pushes arrive only when a run is loaded, and
the connection is quiet in between — and it reconnects only after a real failure, with a slow,
doubling backoff. A read timeout or a fast retry loop would flood the operator's screen.
Guarded by `test_silence_does_not_trigger_a_reconnect` and
`test_reconnect_delay_doubles_to_the_cap` in `tests/test_class_code_client.py`.

## Class codes are withheld, never guessed

The relay forwards the pushed fields raw and never derives a code from a class name: across
one season the mapping from names to codes is many-to-many. A record without a code is
dropped, and the join to rMonitor competitors lives in the server, where a miss is blank.
Guarded by `test_record_entry_skips_short_or_codeless_records` in
`tests/test_class_code_client.py`, and by
`test_class_code_is_blank_when_number_and_class_match_disagreeing_codes`,
`test_class_code_is_never_matched_on_a_class_name_prefix` and
`test_class_code_transponder_match_requires_the_same_class` in `tests/test_race_state.py`.
