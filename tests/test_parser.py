"""Tests for rMonitor protocol parsing.

Message formats are verified against the protocol as implemented by:
  - https://github.com/only-entertainment/rmonitor
  - https://github.com/zacharyfox/RMonitorLeaderboard
"""

import pytest

from app.rmonitor_client import parse_line


# -- $F Heartbeat -----------------------------------------------------------

def test_heartbeat():
    msg = parse_line('$F,14,"00:12:45","13:34:23","00:09:47","Green "')
    assert msg is not None
    assert msg["type"] == "heartbeat"
    assert msg["laps_to_go"] == "14"
    assert msg["time_to_go"] == "00:12:45"
    assert msg["time_of_day"] == "13:34:23"
    assert msg["race_time"] == "00:09:47"
    assert msg["flag"] == "Green"


def test_heartbeat_flag_trim():
    msg = parse_line('$F,9999,"00:00:00","07:59:59","00:00:00","Green "')
    assert msg["flag"] == "Green"


# -- $A Competitor ----------------------------------------------------------

def test_competitor_a():
    msg = parse_line('$A,"1234BE","12X",52474,"John","Johnson","USA",5')
    assert msg is not None
    assert msg["type"] == "competitor"
    assert msg["reg_number"] == "1234BE"
    assert msg["number"] == "12X"
    assert msg["transponder"] == "52474"
    assert msg["first_name"] == "John"
    assert msg["last_name"] == "Johnson"
    assert msg["nationality"] == "USA"
    assert msg["class_number"] == "5"


# -- $COMP Extended competitor ----------------------------------------------

def test_competitor_comp():
    msg = parse_line('$COMP,"21","21",1,"Farnbacher /","James","Panoz Esperante",""')
    assert msg is not None
    assert msg["type"] == "competitor"
    assert msg["reg_number"] == "21"
    assert msg["number"] == "21"
    assert msg["class_number"] == "1"
    assert msg["first_name"] == "Farnbacher /"
    assert msg["last_name"] == "James"
    assert msg["nationality"] == "Panoz Esperante"
    assert msg["additional_data"] == ""


# -- $B Run information -----------------------------------------------------

def test_run():
    msg = parse_line('$B,32,"Test Session 4"')
    assert msg is not None
    assert msg["type"] == "run"
    assert msg["unique_number"] == "32"
    assert msg["description"] == "Test Session 4"


# -- $C Class information ---------------------------------------------------

def test_class_info():
    msg = parse_line('$C,1,"Formula 100"')
    assert msg is not None
    assert msg["type"] == "class_info"
    assert msg["unique_number"] == "1"
    assert msg["description"] == "Formula 100"


# -- $E Setting information -------------------------------------------------

def test_setting_trackname():
    msg = parse_line('$E,"TRACKNAME","Sebring International Raceway"')
    assert msg is not None
    assert msg["type"] == "setting"
    assert msg["description"] == "TRACKNAME"
    assert msg["value"] == "Sebring International Raceway"


def test_setting_tracklength():
    msg = parse_line('$E,"TRACKLENGTH","3.700"')
    assert msg["type"] == "setting"
    assert msg["value"] == "3.700"


# -- $G Race / position information -----------------------------------------

def test_race_info():
    msg = parse_line('$G,1,"21",,"00:00:56.665"')
    assert msg is not None
    assert msg["type"] == "race_info"
    assert msg["position"] == "1"
    assert msg["reg_number"] == "21"
    assert msg["laps"] == ""
    assert msg["total_time"] == "00:00:56.665"


# -- $H Practice / qualifying -----------------------------------------------

def test_qual_info():
    msg = parse_line('$H,1,"21",0,"00:59:59.999"')
    assert msg is not None
    assert msg["type"] == "qual_info"
    assert msg["position"] == "1"
    assert msg["reg_number"] == "21"
    assert msg["best_lap"] == "0"
    assert msg["best_lap_time"] == "00:59:59.999"


# -- $I Init record ---------------------------------------------------------

def test_init():
    msg = parse_line('$I,"10:03:08","27 Jan 09"')
    assert msg is not None
    assert msg["type"] == "init"
    assert msg["time_of_day"] == "10:03:08"
    assert msg["date"] == "27 Jan 09"


# -- $J Passing information -------------------------------------------------

def test_passing():
    msg = parse_line('$J,"21","00:02:06.403","00:04:34.359"')
    assert msg is not None
    assert msg["type"] == "passing"
    assert msg["reg_number"] == "21"
    assert msg["lap_time"] == "00:02:06.403"
    assert msg["total_time"] == "00:04:34.359"


def test_passing_zero():
    msg = parse_line('$J,"21","00:00:00.000","00:00:56.665"')
    assert msg["lap_time"] == "00:00:00.000"


# -- $SP/$SR Lap information (undocumented) ----------------------------------

def test_lap_info_sp():
    msg = parse_line('$SP,1,"21",3,"00:01:45.123"')
    assert msg is not None
    assert msg["type"] == "lap_info"
    assert msg["position"] == "1"
    assert msg["reg_number"] == "21"
    assert msg["lap_number"] == "3"
    assert msg["lap_time"] == "00:01:45.123"


def test_lap_info_sr():
    msg = parse_line('$SR,2,"45",5,"00:02:10.456"')
    assert msg is not None
    assert msg["type"] == "lap_info"


# -- Unknown / malformed lines -----------------------------------------------

def test_unknown_command():
    msg = parse_line('$Z,1,2,3')
    assert msg is None


def test_empty_line():
    msg = parse_line('')
    assert msg is None
