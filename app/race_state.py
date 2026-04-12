"""In-memory race state built from rMonitor messages.

State is populated by the parsed message dicts produced by
``rmonitor_client.parse_line``.  Field names follow the AMB RMonitor Timing
Protocol as implemented by:
  - https://github.com/only-entertainment/rmonitor
  - https://github.com/zacharyfox/RMonitorLeaderboard
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _lap_time_seconds(t: str) -> float | None:
    """Convert ``HH:MM:SS.mmm`` to total seconds, or *None*."""
    if not t:
        return None
    try:
        parts = t.split(":")
        if len(parts) == 3:
            h, m, rest = parts
            s = float(rest)
            return int(h) * 3600 + int(m) * 60 + s
        if len(parts) == 2:
            m, rest = parts
            s = float(rest)
            return int(m) * 60 + s
        return float(parts[0])
    except (ValueError, IndexError):
        return None


class RaceState:
    """Holds the current state of the race, updated by parsed messages."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.competitors: dict[str, dict] = {}  # keyed by reg_number
        self.classes: dict[str, str] = {}  # class_number -> description
        self.track_name: str = ""
        self.track_length_miles: float | None = None
        self.run_description: str = ""
        self.flag: str = ""
        self.race_time: str = ""
        self.time_of_day: str = ""
        self.time_to_go: str = ""
        self.laps_to_go: str = ""
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    def mark_clean(self):
        self._dirty = False

    def process(self, msg: dict) -> str | None:
        """Apply *msg* to the state.  Return an event type string if the UI
        should be notified, or *None* for silent updates."""
        handler = self._HANDLERS.get(msg.get("type"))
        if handler:
            return handler(self, msg)
        return None

    # ---- handlers ----

    def _heartbeat(self, msg: dict) -> str | None:
        changed = (
            self.flag != msg["flag"]
            or self.race_time != msg["race_time"]
        )
        self.flag = msg["flag"]
        self.race_time = msg["race_time"]
        self.time_of_day = msg["time_of_day"]
        self.time_to_go = msg.get("time_to_go", "")
        self.laps_to_go = msg["laps_to_go"]
        if changed:
            self._dirty = True
            return "heartbeat"
        return None

    def _competitor(self, msg: dict) -> str:
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        if msg.get("first_name"):
            c["first_name"] = msg["first_name"]
        if msg.get("last_name"):
            c["last_name"] = msg["last_name"]
        if msg.get("number"):
            c["number"] = msg["number"]
        if msg.get("nationality"):
            c["nationality"] = msg["nationality"]
        if msg.get("class_number"):
            c["class_number"] = msg["class_number"]
        if msg.get("additional_data"):
            c["additional_data"] = msg["additional_data"]
        self._dirty = True
        return "competitor"

    def _run(self, msg: dict) -> str:
        self.run_description = msg["description"]
        self._dirty = True
        return "run"

    def _class_info(self, msg: dict) -> str:
        self.classes[msg["unique_number"]] = msg["description"]
        self._dirty = True
        return "class_info"

    def _setting(self, msg: dict) -> str | None:
        desc = msg["description"].upper()
        if "NAME" in desc:
            self.track_name = msg["value"]
            self._dirty = True
            return "setting"
        if "LENGTH" in desc:
            try:
                self.track_length_miles = float(msg["value"])
            except (ValueError, TypeError):
                self.track_length_miles = None
            self._dirty = True
            return "setting"
        return None

    def _race_info(self, msg: dict) -> str:
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        c["position"] = msg["position"]
        if msg.get("laps"):
            c["laps"] = msg["laps"]
        if msg.get("total_time"):
            c["total_time"] = msg["total_time"]
        self._dirty = True
        return "race_info"

    def _qual_info(self, msg: dict) -> str:
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        if msg.get("best_lap_time"):
            c["best_lap_time"] = msg["best_lap_time"]
        if msg.get("best_lap"):
            c["best_lap"] = msg["best_lap"]
        self._dirty = True
        return "qual_info"

    def _passing(self, msg: dict) -> str:
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        if msg.get("lap_time"):
            c["last_lap_time"] = msg["lap_time"]
            secs = _lap_time_seconds(msg["lap_time"])
            if secs and secs > 0 and self.track_length_miles:
                c["last_lap_speed_mph"] = round(
                    self.track_length_miles / (secs / 3600), 2
                )
            else:
                c["last_lap_speed_mph"] = None
        if msg.get("total_time"):
            c["total_time"] = msg["total_time"]
        self._dirty = True
        return "passing"

    def _lap_info(self, msg: dict) -> str:
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        if msg.get("lap_time"):
            c["last_lap_time"] = msg["lap_time"]
            secs = _lap_time_seconds(msg["lap_time"])
            if secs and secs > 0 and self.track_length_miles:
                c["last_lap_speed_mph"] = round(
                    self.track_length_miles / (secs / 3600), 2
                )
            else:
                c["last_lap_speed_mph"] = None
        if msg.get("lap_number"):
            c["laps"] = msg["lap_number"]
        self._dirty = True
        return "lap_info"

    def _init(self, _msg: dict) -> str:
        log.info("New race/session – clearing all state")
        self.reset()
        return "init"

    _HANDLERS: dict = {}

    # ---- serialisation ----

    def snapshot(self) -> dict:
        """Return the full state as a JSON-serialisable dict."""
        entries = sorted(
            self.competitors.values(),
            key=lambda c: _sort_key(c),
        )
        # Resolve class descriptions
        for e in entries:
            cn = e.get("class_number", "")
            e["class_description"] = self.classes.get(cn, "")
        return {
            "track_name": self.track_name,
            "track_length_miles": self.track_length_miles,
            "run_description": self.run_description,
            "flag": self.flag,
            "race_time": self.race_time,
            "time_of_day": self.time_of_day,
            "time_to_go": self.time_to_go,
            "laps_to_go": self.laps_to_go,
            "entries": entries,
        }


# Register handlers after class body is complete
RaceState._HANDLERS = {
    "heartbeat": RaceState._heartbeat,
    "competitor": RaceState._competitor,
    "run": RaceState._run,
    "class_info": RaceState._class_info,
    "setting": RaceState._setting,
    "race_info": RaceState._race_info,
    "qual_info": RaceState._qual_info,
    "passing": RaceState._passing,
    "lap_info": RaceState._lap_info,
    "init": RaceState._init,
}


def _empty_competitor(reg: str) -> dict:
    return {
        "reg_number": reg,
        "number": reg,
        "first_name": "",
        "last_name": "",
        "nationality": "",
        "additional_data": "",
        "class_number": "",
        "position": "",
        "laps": "",
        "total_time": "",
        "last_lap_time": "",
        "last_lap_speed_mph": None,
        "best_lap_time": "",
        "best_lap": "",
    }


def _sort_key(c: dict):
    """Sort competitors by position (numeric), unknowns last."""
    try:
        return (0, int(c["position"]))
    except (ValueError, TypeError):
        return (1, c.get("number", ""))
