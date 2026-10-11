# The `:51738` results view: columns and time corrections

Learned from a capture on 2026-10-11, while the operator added a manual 5 s time correction
for track limits to two cars in a race. Only what is listed here has been checked.

## Frames

`lgView_RaceResults` (`RESULTS_VIEW` in `relay/class_code_client.py`) answers a subscription
with a `21 80` column frame, then the `24 80` reply that `EntryListParser` reads. The column
frame lists each column as:

    str   name           e.g. lgHeader_HasPenalties, lgobsHeader_TotalTimeCorrection
    u32   width          display pixels
    u16   flags
    u32   index          the column's position in a row
    u8    group          the column group (General, Competitor, Lap, Result, Correction, Pit, ...)
    u8    format
    u32   0

The `lgLabel_*_Columns` entries are group titles, with a different layout.

## Cells in a row

Cells run in column-index order. `EntryListParser` reads a row's head and tail and skips the
rest; the lap, time and correction columns come after the tail. The cells checked so far are a
raw value, one byte, then the display text as a `str`:

- times and corrections: `i64` microseconds (`5000000` shows as `5.000`)
- counts and booleans: `i32`
- speeds: `f64`

An empty cell has a zero value and an empty `str`.

## Columns 57–67

| Index | Name | Seen |
|---|---|---|
| 57 | `lgHeader_RMFlags` | `81920` mid-race |
| 58 | `lgHeader_LapsLed` | |
| 59 | `lgHeader_HasPassings` | 1 once the car has a passing |
| 60 | `lgHeader_HasManuals` | 0 |
| 61 | `lgHeader_IsFinished` | goes to 1 as each car takes the chequered flag |
| 62 | `lgHeader_HasPenalties` | **0 throughout, including on corrected cars** |
| 63 | `lgobsHeader_TotalTimeCorrection` | `5.000` on corrected cars, empty otherwise |
| 64 | `lgobsHeader_BestLapTimeCorrection` | empty |
| 65 | `lgobsHeader_LapsCorrection` | empty |
| 66 | `lgobsHeader_PointsCorrection` | empty |
| 67 | `lgobsHeader_PositionCorrection` | |

## A time correction is the operator's penalty

A manual time correction shows up in three places, and only one of them marks it as a correction:

- rMonitor `$G` on `:50000`: the race time grows by the correction and the order is recalculated,
  with no marker. For one car this came 16 s before the correction appeared in a results reply.
- `TotalTimeCorrection` above zero on the car's row in the results view (the next reply, as the
  relay asks for one every 60 s).
- Usually an announcement row from race control ("Racetime includes 5 second time penalty"),
  typed separately, which can arrive before or after the correction.

So detect a time penalty by `TotalTimeCorrection > 0`, never by `HasPenalties`.

## An added lap

Orbits' add-a-lap penalty, applied for about 5 s before a start, showed in `$G` as the car's
lap count going up by one and its position recalculated, again with no marker. On `:51738`
each edit, on and off, raised a "Competitor modified" push with the same fields both times and
a "Recalculate forced on '<run>'" notice. That notice follows almost every edit to a run, so it
marks no penalty. The edit fell between two results replies, so whether `HasPenalties` or
`LapsCorrection` reflects an added lap is still unknown: hold one for over 60 s to see a reply.
