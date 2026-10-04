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
