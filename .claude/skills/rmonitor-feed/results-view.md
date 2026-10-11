# The `:51738` results view: columns and time corrections

Learned from captures on 2026-10-11, one of them while the operator added a manual 5 s time
correction for track limits to two cars in a race. Every column below was decoded from the
202 results replies (2528 rows) in `capture_20261011T112147_both.pcap`. Only what is listed
here has been checked.

## Frames

`lgView_RaceResults` (`RESULTS_VIEW` in `relay/class_code_client.py`) answers a subscription
with a `21 80` column frame, then the `24 80` reply that `EntryListParser` reads. The relay
opens a new view for each poll (every 60 s) and gets exactly those two frames per view, with
no pushes in between, so every value here is only as fresh as the last poll.

The column frame's block, after the u32 block length:

    u32   view id
    u32   10             group count
    10 x  u8 group, str label     0 lgLabel_General_Columns … 9 lgLabel_Best_Section_Columns
    u32   87             column count
    87 x  u32   index        0-based, the column's position in a row
          u8    group
          u8    format       how the cell is encoded (below)
          str   caption      the operator's name for a CarAdditional or section column, else empty
          str   name         e.g. lgHeader_Gap, lgobsHeader_TotalTimeCorrection
          u32   width        display pixels
          u16   flags

The column set was the same in all 212 column frames captured.

## Rows

Each row in the reply starts with 14 bytes not yet decoded, then one cell per column in index
order. A cell's layout follows its column's format:

| Format | Cell | Holds |
|---|---|---|
| 0, 1 | `i32`, 1 byte, `str` | integers, counts, flags |
| 2 | `f64`, 1 byte, `str` | speeds (mph), distance (miles), points |
| 3 | `str`, 1 byte | text |
| 4, 6 | `i64`, 1 byte, `str` | times in microseconds; 6 is a signed difference |
| 5 | `i64`, `i64`, 4 bytes, `str` | stint and live section times |
| 9 | `u32` n, 1 byte, n × `u32`, `str` | transponder list |

The `str` is the text the Orbits client displays. The single byte is a display style: after
the value in formats 0, 1, 2, 4 and 6, after the text in format 3, and after the count in
format 9. Format 5's four bytes are not decoded and no style byte is known for it. The style
probably selects one of the Orbits Display theme's special colours (`overallbest`,
`personalbest`, `better`, `worse`, `inleadlap`, …). Counted over every row:

- `4`: an overall best. `BestTm` carries it only on the session's fastest lap, and `LastTm`
  when the last lap is that lap.
- `3`: a personal best. `LastTm` carries it only when the last lap equals the car's `BestTm`.
- `5` and `6`: better and worse. `6` appears only on `Diff`, `Gap` and `GapBehind`. On `Gap`,
  `5` came with a gap smaller than the previous reply's in 340 of 401 cases and `6` with a
  larger one in 515 of 585; the reference is likely the previous lap, not the previous 60 s
  poll.
- `16`: `LastTm` is `5` when quicker than `2ndLastTm` and `16` when slower (937 of 937), never
  `6`. `16` also fills `Laps`, `No` and the section columns, so it is likely a plain style
  rather than "worse".
- `0`, `15`, `20` are not decoded.

In a time cell, an `i64` of
`0x7fffffffffffffff` means no value, and time-of-day values count microseconds from 1970 in
local wall-clock time (`1791734050115000` shows as `15:54:10.115` on a BST day).

`EntryListParser` reads only the head (`No` to `Class`), the status, and the tail
(`CarAdditional0` to `Car_Transponders`) by walking strings; its "ten zero bytes" after the
status are column 13's style byte and column 14's empty `MinimumLaptime` value and style byte.

## Gap and Diff are in the results view

Orbits sends the gaps its own displays show: the track's Orbits Display race sequence
(`Race - SMART 12a.xseq`) is a table on `lgView_RaceResults` with `lgHeader_Diff` and
`lgHeader_Gap` beside `TotalTm`, `BestTm` and `LastTm`. Columns 47, 48 and 53 share one encoding: the
`i64` holds microseconds, and a lap deficit sets `laps << 48` (the low bits may still hold a
time). The text is `1.475`, `1:23.454` past a minute, `1 Lap` or `2 Laps`; the leader's
`Diff` and `Gap` are empty. Orbits shows `1 Lap` from a one-lap deficit, where the server's own
columns wait for two (`_apply_intervals` in `server/race_state.py`). A DNF or DNS car's `Diff`
reads `DNF` or `DNS` with a zero value.

In the KMSC Legends race, each `Diff` is the car's `TotalTm` minus the leader's (10:59.476 −
10:58.001 = 1.475) and equals the sum of the `Gap`s above it, so they move at start/finish
crossings, not at the intermediate passings in `LastPassingTime`.

## Columns

Groups: 0 General, 1 Competitor, 2 Laptime, 3 Lap, 4 Result, 5 Correction, 6 Pit,
7 Section, 8 Live section, 9 Best section.

| Index | Name | Group | Format | Seen |
|---|---|---|---|---|
| 0 | `lgHeader_Pos` | 0 | 0 | `1`, `2`, … |
| 1 | `lgHeader_PosInClass` | 0 | 0 | `1`, `2`, … |
| 2 | `lgHeader_Marker` | 0 | 0 | `0`–`5` |
| 3 | `lgHeader_No` | 1 | 3 | `127` |
| 4 | `lgHeader_First_Name` | 1 | 3 | `Phil` |
| 5 | `lgHeader_Last_Name` | 1 | 3 | `ROWLANDS` |
| 6 | `lgHeader_Name` | 1 | 3 | `Phil ROWLANDS` |
| 7 | `lgHeader_Class` | 1 | 3 | `KMSC Legends` |
| 8 | `lgHeader_Points` | 1 | 2 | `0`; `200`, `185`, … after a race |
| 9 | `lgHeader_Comment` | 1 | 3 | always empty |
| 10 | `lgHeader_Start_Pos` | 1 | 0 | `1`, `2`, … |
| 11 | `lgHeader_Starting_Grid_Comment` | 1 | 3 | a qualifying time (`56.462`) or empty |
| 12 | `lgHeader_IsHidden` | 1 | 0 | `0` |
| 13 | `lgHeader_Status` | 1 | 3 | `Active`, `DNF`, `DNS` |
| 14 | `lgHeader_MinimumLaptime` | 1 | 4 | `0.000` |
| 15–24 | `lgHeader_CarAdditional0`–`9` | 1 | 3 | captions `Make/Model`, `CC`, `Class`, `Newcomer`, `Club`, `Engine`, `Tyres`, `Team`, `Starting grid comment`, `lgHeader_Caused_Accident`; only 0–2 filled (`Legend Coupe`, `890`, `SC`) |
| 25 | `lgHeader_Car_RegNumber` | 1 | 3 | `7e74b1bc` |
| 26 | `lgHeader_Driver_RegNumber` | 1 | 3 | `9654aa5c` |
| 27 | `lgHeader_Car_Transponders` | 1 | 9 | `713362` |
| 28 | `lgHeader_Label` | 1 | 3 | `PRO` |
| 29 | `lgHeader_Main_Transponder` | 1 | 0 | `-` |
| 30 | `lgHeader_BestTm` | 2 | 4 | `59.836` |
| 31 | `lgHeader_BestSpd` | 2 | 2 | `76.222` |
| 32 | `lgHeader_In_Lap` | 2 | 1 | lap of the best time, `5` |
| 33 | `lgHeader_BestTOD` | 2 | 4 | `15:50:43.814` |
| 34 | `lgHeader_2nd_Best` | 2 | 4 | `59.931` |
| 35 | `lgHeader_2nd_Spd` | 2 | 2 | `76.102` |
| 36 | `lgHeader_2nd_Lap` | 2 | 1 | `7` |
| 37 | `lgHeader_LastTm` | 3 | 4 | `1:26.273` |
| 38 | `lgHeader_LastSpd` | 3 | 2 | `52.865` |
| 39 | `lgHeader_Laps` | 3 | 1 | `8` |
| 40 | `lgHeader_2ndLastTm` | 3 | 4 | `59.931` |
| 41 | `lgHeader_3thLastTm` | 3 | 4 | `1:00.097` |
| 42 | `lgHeader_AvgTm` | 3 | 4 | `1:22.162` |
| 43 | `lgHeader_Pace` | 3 | 4 | `1:08.097` |
| 44 | `lgHeader_TotalTm` | 4 | 4 | `10:58.001` |
| 45 | `lgHeader_TotalDistance` | 4 | 2 | `10.135` |
| 46 | `lgHeader_AvgSpeed` | 4 | 2 | `55.451` |
| 47 | `lgHeader_Diff` | 4 | 6 | to the leader: `1.475`, `1 Lap`, `DNF` |
| 48 | `lgHeader_Gap` | 4 | 6 | to the car ahead: `0.757`, `1 Lap` |
| 49 | `lgHeader_LastTmDiff` | 4 | 6 | `LastTm` − `2ndLastTm`: `26.342`, `-0.147` |
| 50 | `lgHeader_TimeOfDay` | 4 | 4 | last start/finish crossing, `15:54:10.115` |
| 51 | `lgHeader_LastPassingTimeline` | 4 | 0 | value is a loop id (`2817`), text is `Start/Finish`, `Intermediate 2 Speed`, … |
| 52 | `lgHeader_LastPassingTime` | 4 | 4 | `15:56:04.584` |
| 53 | `lgHeader_GapBehind` | 4 | 6 | to the car behind: `1.475` |
| 54 | `lgHeader_NumberBehind` | 4 | 3 | that car's number, `67` |
| 55 | `lgHeader_PersBestDiff` | 4 | 6 | `LastTm` − `BestTm`: `26.437` |
| 56 | `lgHeader_RMFlags` | 4 | 0 | `81920`, `147456` mid-race |
| 57 | `lgHeader_LapsLed` | 4 | 0 | `8` |
| 58 | `lgHeader_HasPassings` | 4 | 0 | 1 once the car has a passing |
| 59 | `lgHeader_HasManuals` | 4 | 0 | 0 |
| 60 | `lgHeader_IsFinished` | 4 | 0 | goes to 1 as each car takes the chequered flag |
| 61 | `lgHeader_HasPenalties` | 5 | 0 | **0 throughout, including on corrected cars** |
| 62 | `lgobsHeader_TotalTimeCorrection` | 5 | 4 | `5.000` on corrected cars, empty otherwise |
| 63 | `lgobsHeader_BestLapTimeCorrection` | 5 | 4 | empty |
| 64 | `lgobsHeader_LapsCorrection` | 5 | 1 | empty |
| 65 | `lgobsHeader_PointsCorrection` | 5 | 2 | `-` |
| 66 | `lgobsHeader_PositionCorrection` | 5 | 1 | empty |
| 67 | `lgHeader_NoPitsStops` | 6 | 0 | empty |
| 68 | `lgHeader_LastPitLap` | 6 | 1 | `0` |
| 69 | `lgHeader_SincePit` | 6 | 1 | laps, `8` |
| 70 | `lgHeader_StintTime` | 6 | 5 | `12:52` |
| 71 | `lgHeader_TotalStintTime` | 6 | 5 | `12:52` |
| 72–74 | `lgHeader_Sec_0`–`2` | 7 | 4 | captions `S1`–`S3`, `50.106` |
| 75–76 | `lgHeader_Sec_3`–`4` | 7 | 2 | captions `S3Spd`, `SFSpd`, `60.2` |
| 77–81 | `lgobsHeader_Live_Sec_0`–`4` | 8 | 5 | captions `S1 live` … `SFSpd live`; 80–81 show short times (`0.788`) despite the speed captions |
| 82–84 | `lgHeader_SecBest_0`–`2` | 9 | 4 | captions `S1 Best`–`S3 Best`, `19.397` |
| 85–86 | `lgHeader_SecBest_3`–`4` | 9 | 2 | captions `S3Spd Best`, `SFSpd Best`, `98.1` |

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
