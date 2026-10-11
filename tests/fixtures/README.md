# Test fixtures

## `model_pull_excerpt.bin`

A scrubbed excerpt of a model pull captured from the timing host's `:51738` on the open
timing network on 2026-10-04. It is five byte-for-byte slices of that pull, in the pull's
own order, joined end to end. The slices were chosen to hold run-table records next to the
non-run records an anchor that is too loose could mistake for runs. The guard tests are in
`tests/test_class_code_client.py` (`test_model_excerpt_*`).

| Fixture bytes      | Holds                                                         |
|--------------------|---------------------------------------------------------------|
| `0x0000`–`0x0121`  | Timing-loop table: its IEEE-754 doubles at `0x0061` form a run header's shape |
| `0x0121`–`0x028B`  | Four group records, `0x800009D3`–`0x800009D6`                  |
| `0x028B`–`0x03B6`  | Three run records, one nameless                               |
| `0x03B6`–`0x1072`  | Twenty run records, with the settings between them            |
| `0x1072`–`0x1222`  | Two competitor registry records                               |

Every string was overwritten in place with a placeholder of the same length, so each length
prefix still holds. Run names became `Run NN`, group names `Group NN`, and every other text
became `TextNNN`, `xxx` or zeros, padded with `.`. Registration ids became `aaaaNNNN` and
transponders `10000NN`. Ids, flags, timestamps, one- and two-digit numbers and all binary
fields were kept. Re-scrub any replacement the same way:
`test_model_excerpt_holds_only_placeholder_text` fails on any other printable text.

### Run records

A run record starts with 16 bytes whose content varies, followed by the run id, u32 flags,
the group id and the name as a length-prefixed string (see `parse_runs`). In the table,
*Offset* is the offset of the run id and *Lead-in* describes the 16 bytes in front of it:
`u32 1` means they start `01 00 00 00`; `zeros` means all 16 are zero. The parser's old
anchor required `u32 1`, so it missed the three `zeros` records (#108).

| Offset   | Lead-in | Run id       | Flags   | Group id     | Name                   |
|----------|---------|--------------|---------|--------------|------------------------|
| `0x029B` | u32 1   | `0x400032B1` | `0x234` | `0x800008F1` | `Run 01.`              |
| `0x0305` | u32 1   | `0x400032B2` | `0x030` | `0x800008F2` | (empty)                |
| `0x035C` | zeros   | `0x400032B3` | `0x234` | `0x800008F3` | `Run 02.`              |
| `0x03C6` | u32 1   | `0x400035D9` | `0x23C` | `0x800009D3` | `Run 03.`              |
| `0x0430` | u32 1   | `0x400035DA` | `0x23C` | `0x800009D3` | `Run 04......`         |
| `0x049F` | u32 1   | `0x400035DB` | `0x23C` | `0x800009D3` | `Run 05...........`    |
| `0x0599` | u32 1   | `0x400035DC` | `0x23C` | `0x800009D3` | `Run 06.`              |
| `0x0603` | u32 1   | `0x400035DD` | `0x23E` | `0x800009D3` | `Run 07............`   |
| `0x06FE` | u32 1   | `0x400035DE` | `0x23C` | `0x800009D4` | `Run 08.`              |
| `0x0768` | u32 1   | `0x400035DF` | `0x23C` | `0x800009D4` | `Run 09......`         |
| `0x07D7` | u32 1   | `0x400035E0` | `0x23C` | `0x800009D4` | `Run 10...........`    |
| `0x08D1` | u32 1   | `0x400035E1` | `0x23C` | `0x800009D4` | `Run 11.`              |
| `0x093B` | u32 1   | `0x400035E2` | `0x038` | `0x800009D4` | `Run 12............`   |
| `0x0A2A` | zeros   | `0x400035E3` | `0x23C` | `0x800009D5` | `Run 13.`              |
| `0x0A94` | u32 1   | `0x400035E4` | `0x23C` | `0x800009D5` | `Run 14......`         |
| `0x0B03` | u32 1   | `0x400035E5` | `0x23C` | `0x800009D5` | `Run 15...........`    |
| `0x0BFD` | u32 1   | `0x400035E6` | `0x23C` | `0x800009D5` | `Run 16.`              |
| `0x0C67` | u32 1   | `0x400035E7` | `0x038` | `0x800009D5` | `Run 17............`   |
| `0x0D56` | zeros   | `0x400035E8` | `0x23C` | `0x800009D6` | `Run 18......`         |
| `0x0DC5` | u32 1   | `0x400035E9` | `0x23C` | `0x800009D6` | `Run 19.`              |
| `0x0E2F` | u32 1   | `0x400035EA` | `0x23C` | `0x800009D6` | `Run 20...........`    |
| `0x0F29` | u32 1   | `0x400035EB` | `0x23C` | `0x800009D6` | `Run 21.`              |
| `0x0F93` | u32 1   | `0x400035EC` | `0x038` | `0x800009D6` | `Run 22............`   |

`0x400035E8` is the run from #108, a qualifying session that the old anchor missed. The
three `zeros` records are each the first run of their group in this excerpt.

## `results_view_excerpt.bin`

One scrubbed results-view reply captured from the timing host's `:51738` on 2026-10-11:
the reply to a `lgView_RaceResults` subscription for a finished race, view id 2, whole and
unchanged in length. The frame's 63-byte header starts at `0x0000` (opcode `24 80`), the
view id at `0x003F`, the row count (14) at `0x006F`. The guard tests are in
`tests/test_class_code_client.py` (`test_results_excerpt_*`).

Each row's status is the `str` in its middle followed by ten zero bytes and a time `str`.
*Head* is the offset of the row's number `str`, *Status* that of its status `str`.

| Head     | Status   | Number | Class                  | Status |
|----------|----------|--------|------------------------|--------|
| `0x009F` | `0x0118` | 64     | SMRC Classic S/S (E)   | Active |
| `0x05BF` | `0x0639` | 60     | SMRC Classic S/S (I)   | Active |
| `0x0AEF` | `0x0B68` | 63     | SMRC Classic S/S (E)   | Active |
| `0x1021` | `0x109E` | 51     | SMRC Classic S/S (E)   | Active |
| `0x1557` | `0x15D6` | 311    | SMRC Classic S/S (I)   | Active |
| `0x1A98` | `0x1B14` | 73     | SMRC Classic S/S (I)   | Active |
| `0x1FD4` | `0x204B` | 21     | SMRC Classic S/S (G)   | Active |
| `0x250E` | `0x258F` | 32     | SMRC Classic S/S (B)   | Active |
| `0x2A52` | `0x2AD4` | 13     | SMRC Classic S/S (A)   | Active |
| `0x2F89` | `0x3007` | 11     | SMRC Classic S/S (A)   | Active |
| `0x34D4` | `0x354C` | 77     | SMRC Classic S/S (C)   | Active |
| `0x3A0E` | `0x3A91` | 89     | SMRC Classic S/S (I)   | Active |
| `0x3F4F` | `0x3FC7` | 71     | SMRC Classic S/S (A)   | DNF    |
| `0x445E` | `0x44D3` | 45     | SMRC Classic S/S (A)   | DNS    |

Every string was overwritten in place with a placeholder of the same length. Names, car
models and initials became `TextNNN` (padded with `.`) or `x`s; car and driver registrations
became `aaaa` and digits; transponders became `10000NN` or `1000NN`, in their text field and
in the u32 before it. Car numbers, class names, class codes, statuses, times and the host's
labels (`Not classified`, `Start/Finish`, `Intermediate N`, `RETIRED`, `1 Lap`) were kept.
Re-scrub any replacement the same way: `test_results_excerpt_holds_only_placeholder_text`
fails on any other length-prefixed text.
