"""In-memory race state built from rMonitor messages.

State is populated by the parsed message dicts produced by
``rmonitor_client.parse_line``.  Field names follow the AMB RMonitor Timing
Protocol as implemented by:
  - https://github.com/only-entertainment/rmonitor
  - https://github.com/zacharyfox/RMonitorLeaderboard
"""

from __future__ import annotations

import logging
import time

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
    except (ValueError, IndexError, AttributeError, TypeError):
        return None


# Some Orbits setups emit this in place of an empty cumulative time: it appears
# 37 times in the ``$G`` lines of ``examples/2009 Sebring Test ALMS Session 4 -
# 0800-1000.txt`` as a "no time set" marker rather than a real elapsed time.
# Read as elapsed seconds it would put a car an hour behind the leader.
_NO_TIME_SENTINEL = "00:59:59.999"


def _interval_seconds(value: str) -> float | None:
    """Convert a feed time field to seconds, or *None* if unusable.

    The single input guard for both session modes.  In a race
    :meth:`RaceState._race_info` passes a cumulative ``total_time``; in
    qualifying :meth:`RaceState._apply_intervals` passes a ``best_lap_time``.
    Both want the same four checks and differ only in which field they read, so
    neither branch may reach the interval derivation by another route.

    :param value: a time field as the feed sends it, ``"00:14:33.950"``.
    :returns: seconds, or *None* for an empty value, for the
        ``_NO_TIME_SENTINEL`` marker, for anything :func:`_lap_time_seconds`
        cannot parse, and for a non-positive result.

    The zero guard mirrors the rule :func:`_sort_key` already applies: a zero
    ``total_time`` means the competitor has not crossed the timing line, not
    that they crossed it at time zero.
    """
    if not value:
        return None
    if str(value).strip() == _NO_TIME_SENTINEL:
        return None
    secs = _lap_time_seconds(value)
    if secs is None or secs <= 0:
        return None
    return secs


def _lap_number(laps: str) -> int | None:
    """Convert a ``laps`` field to a completed-lap count, or *None*.

    :param laps: a competitor's completed-lap count as the feed sends it.
    :returns: the lap number, or *None* when the field is empty, non-numeric or
        non-positive — zero completed laps gives nothing to compare against.
    """
    try:
        n = int(laps)
    except (ValueError, TypeError):
        return None
    return n if n > 0 else None


def _coerce_scalars(msg: dict) -> dict:
    """Coerce non-string/None scalar values in an ingest message to str.

    Ingest messages arrive as untrusted JSON from the network. A malformed
    or malicious payload could substitute e.g. an int or list for a field
    the rest of this module treats as a string (flag, total_time, ...),
    which would otherwise crash later in snapshot() rather than at the
    point the bad value entered state. Real relay-sourced messages already
    contain only strings, so this is a no-op for well-formed input.
    """
    return {
        k: v if v is None or isinstance(v, (dict, list)) else str(v)
        for k, v in msg.items()
    }


class RaceState:
    """Holds the current state of the race, updated by parsed messages."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.competitors: dict[str, dict] = {}  # keyed by reg_number
        self.classes: dict[str, str] = {}  # class_number -> description
        self.leader_time_at_lap: dict[int, float] = {}
        self.track_name: str = ""
        self.track_length_miles: float | None = None
        self.run_description: str = ""
        self.flag: str = ""
        self.race_time: str = ""
        self.time_of_day: str = ""
        self.time_to_go: str = ""
        self.laps_to_go: str = ""
        self._is_qualifying: bool = False
        self._seen_race_info: bool = False
        self._dirty = True
        self.last_updated: float = time.time()

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def is_qualifying(self) -> bool:
        return self._is_qualifying

    def mark_clean(self):
        self._dirty = False

    def process(self, msg: dict) -> str | None:
        """Apply *msg* to the state.  Return an event type string if the UI
        should be notified, or *None* for silent updates."""
        handler = self._HANDLERS.get(msg.get("type"))
        if handler:
            event = handler(self, _coerce_scalars(msg))
            if event is not None:
                self.last_updated = time.time()
            return event
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
        """Merge competitor fields from ``$A``/``$COMP`` into one entry.

        Two details that are easy to get wrong:

        - Entries are keyed by ``reg_number``, the internal registration key
          (e.g. ``"21"``), never by ``number``, the *displayed* car number, which
          may carry letters (``"12X"``).
        - A field is written only when the incoming value is non-empty, so a
          later message carrying blanks cannot blank data an earlier one gave.
        """
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
        """Apply a ``$G`` (race information) message.

        ``$G`` is the only message carrying a competitor's lap number and its
        cumulative time together — ``$J`` (:meth:`_passing`) has no lap field
        at all and ``$SP``/``$SR`` (:meth:`_lap_info`) no cumulative time — so
        this is the only point at which a lap-consistent
        ``(timed_lap, timed_lap_seconds)`` pair can be captured.  Both interval
        columns read that pair rather than the live ``laps`` and ``total_time``
        fields, which three handlers write independently of one another.

        The ``>=`` guard on the stamp is **stability, not correctness**: a lap
        regression in this feed is a stale replay of a *still-consistent* pair,
        so a plain last-write would already report a correct interval — one lap
        old, for one snapshot.  Refusing the backward write stops the displayed
        value stepping backwards at all, for the same reason
        :meth:`_record_leader_time` keeps the minimum.
        """
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        _update_position(c, msg["position"])
        if msg.get("laps"):
            c["laps"] = msg["laps"]
        if msg.get("total_time"):
            c["total_time"] = msg["total_time"]
        lap = _lap_number(msg.get("laps", ""))
        secs = _interval_seconds(msg.get("total_time", ""))
        self._record_leader_time(lap, secs)
        if lap is not None and secs is not None:
            # .get(): _load_dict restores competitor dicts verbatim, so a store
            # written before this pair existed has neither key.
            stamped = c.get("timed_lap")
            if stamped is None or lap >= stamped:
                c["timed_lap"] = lap
                c["timed_lap_seconds"] = secs
        self._is_qualifying = False
        self._seen_race_info = True
        self._dirty = True
        return "race_info"

    def _qual_info(self, msg: dict) -> str:
        """Apply a ``$H`` (qualifying information) message.

        Some Orbits setups send ``$H`` *during a race* for best-lap tracking, so
        position and ``_is_qualifying`` are touched only while
        ``_seen_race_info`` is still False — otherwise qualifying positions would
        overwrite the ``$G`` race order.  Best-lap fields update either way.
        """
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        if not self._seen_race_info and msg.get("position"):
            c["position"] = msg["position"]
        if msg.get("best_lap_time"):
            c["best_lap_time"] = msg["best_lap_time"]
        if msg.get("best_lap"):
            c["best_lap"] = msg["best_lap"]
        if not self._seen_race_info:
            self._is_qualifying = True
        self._dirty = True
        return "qual_info"

    def _update_lap_speed(self, competitor: dict, lap_time: str) -> None:
        """Update last lap time and computed speed on *competitor*.

        ``last_lap_speed_mph`` needs both ``track_length_miles`` (from
        ``$E TRACKLENGTH``) and a positive lap time; a zero or missing value on
        either side yields *None* rather than a computed figure.
        """
        competitor["last_lap_time"] = lap_time
        secs = _lap_time_seconds(lap_time)
        if secs and secs > 0 and self.track_length_miles:
            competitor["last_lap_speed_mph"] = round(
                self.track_length_miles * 3600 / secs, 2
            )
        else:
            competitor["last_lap_speed_mph"] = None

    def _record_leader_time(self, lap: int | None, secs: float | None) -> None:
        """Record the earliest elapsed time seen at *lap* completed laps.

        :param lap: a lap number already validated by :func:`_lap_number`, or
            *None* if the ``$G`` carried no usable one.
        :param secs: the matching elapsed time already validated by
            :func:`_interval_seconds`, or *None*.  The caller validates both
            once and uses the same pair for this index and for the
            competitor's own stamp, so the two writes cannot diverge.

        The earliest time at which *any* competitor completed a given lap is by
        definition the leader-on-the-road's time at that lap, which is the
        baseline :meth:`_time_behind_leader` measures every competitor against.

        Two things the code cannot show:

        - Refresh bursts repeat an identical ``(position, reg, lap, time)``
          triple — ``$G,1,"15",8,"00:09:14.151"`` arrives three times in
          ``captures/capture_20260517T134241.log`` — so keeping the minimum is
          what makes recording idempotent, and a slower car reaching the same
          lap later cannot raise the time already recorded for it.
        - ``$G`` is deliberately the sole feed for this index. ``$J``
          (:meth:`_passing`) carries no lap number, so the only lap available
          there is the competitor's ``laps`` field, which ``$J`` does not
          advance; recording against it would seed a lap key with a too-late
          time whenever ``$J`` arrived before the matching ``$G``, silently
          understating every gap at that lap.
        """
        if lap is None or secs is None:
            return
        best = self.leader_time_at_lap.get(lap)
        if best is None or secs < best:
            self.leader_time_at_lap[lap] = secs

    def _time_behind_leader(self, competitor: dict) -> float | None:
        """Seconds *competitor* is behind the leader at its own timed lap.

        Measuring against the leader's time at the competitor's **own** lap is
        what makes two cars on different lap counts comparable at all.
        Subtracting two ``total_time`` values from one snapshot does not: the
        two cars have completed different numbers of laps, so that subtraction
        reports a car that is behind as being ahead.

        Both halves come from the ``(timed_lap, timed_lap_seconds)`` pair
        :meth:`_race_info` stamps, never from the live ``laps`` and
        ``total_time``.  Those two are written by three handlers independently,
        so they are not a pair: a ``$J`` landing a millisecond before its
        matching ``$G`` advances the time without the lap, and because the
        leader's value is the normalisation base that pushes *every* row
        negative for that snapshot.

        A competitor between crossings therefore holds its **previous lap's**
        interval rather than blanking, which is how a real timing screen
        behaves — a gap only moves when a car crosses the line.  That is the
        intended steady state, not a stale cell.

        *None* whenever the pair is missing — a competitor with no ``$G`` yet,
        or a ``data/state.json`` written before the pair existed — or when the
        lap has not been recorded in ``leader_time_at_lap`` yet.
        """
        lap = competitor.get("timed_lap")
        secs = competitor.get("timed_lap_seconds")
        if lap is None or secs is None:
            return None
        leader_secs = self.leader_time_at_lap.get(lap)
        if leader_secs is None:
            return None
        return secs - leader_secs

    def _passing(self, msg: dict) -> str:
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        if msg.get("lap_time"):
            self._update_lap_speed(c, msg["lap_time"])
        if msg.get("total_time"):
            c["total_time"] = msg["total_time"]
        self._dirty = True
        return "passing"

    def _lap_info(self, msg: dict) -> str:
        reg = msg["reg_number"]
        c = self.competitors.setdefault(reg, _empty_competitor(reg))
        if msg.get("lap_time"):
            self._update_lap_speed(c, msg["lap_time"])
        if msg.get("lap_number"):
            c["laps"] = msg["lap_number"]
        if msg.get("position"):
            _update_position(c, msg["position"])
        self._dirty = True
        return "lap_info"

    def _init(self, _msg: dict) -> str:
        log.info("New race/session – clearing all state")
        self.reset()
        return "init"

    _HANDLERS: dict = {
        "heartbeat": _heartbeat,
        "competitor": _competitor,
        "run": _run,
        "class_info": _class_info,
        "setting": _setting,
        "race_info": _race_info,
        "qual_info": _qual_info,
        "passing": _passing,
        "lap_info": _lap_info,
        "init": _init,
    }

    # ---- serialisation ----

    def snapshot(self) -> dict:
        """Return the full state as a JSON-serialisable dict.

        Sort order follows the session: ``best_lap_time`` ascending while
        ``_is_qualifying``, numeric ``position`` otherwise.  A purple flag
        overrides both with ``total_time`` ascending whatever the mode says —
        intended for formation and pace laps at race end, where cars are on
        track in the order they crossed the timing loop.
        """
        flag = str(self.flag).strip().lower()
        if flag == "purple":
            sort_fn = _sort_key_purple
        elif self._is_qualifying:
            sort_fn = _sort_key_best_lap
        else:
            sort_fn = _sort_key
        entries = sorted(
            self.competitors.values(),
            key=lambda c: sort_fn(c),
        )
        # Resolve class descriptions
        for e in entries:
            cn = e.get("class_number", "")
            e["class_description"] = self.classes.get(cn, "")
        self._apply_intervals(entries, purple=(flag == "purple"))
        return {
            "track_name": self.track_name,
            "track_length_miles": self.track_length_miles,
            "run_description": self.run_description,
            "session_mode": self._derive_session_mode(),
            "flag": self.flag,
            "race_time": self.race_time,
            "time_of_day": self.time_of_day,
            "time_to_go": self.time_to_go,
            "laps_to_go": self.laps_to_go,
            "entries": entries,
        }

    def _apply_intervals(self, entries: list[dict], *, purple: bool) -> None:
        """Write the ``Gap`` and ``Diff`` intervals onto every entry.

        *entries* must already be in display order: ``Gap`` reads the entry one
        position ahead and ``Diff`` the first-placed entry, so both are
        whole-state derivations belonging to the :meth:`snapshot` pass rather
        than to a message-time helper.

        Four fields are written on every entry, and only ever one half of each
        pair is non-*None*: ``gap_ahead_seconds``/``gap_ahead_laps`` and
        ``diff_leader_seconds``/``diff_leader_laps``.  The first-placed entry
        keeps all four *None* — it has nobody ahead and is its own reference, so
        both columns render an em-dash rather than ``0.000``.

        Two choices the code cannot explain:

        - **The lap-deficit threshold is 2, not 1.**  A one-lap difference is
          the *normal* state — the car ahead has crossed the line for this lap
          and you have not — so a threshold of 1 would flicker between ``+1 L``
          and a time roughly once per lap.
        - **Seconds are rounded to 3 dp**, matching the feed's millisecond
          resolution.  That is what keeps each ``Diff`` exactly equal to the
          running sum of the ``Gap`` values above it rather than float-noisy.
        """
        for e in entries:
            e["gap_ahead_seconds"] = None
            e["gap_ahead_laps"] = None
            e["diff_leader_seconds"] = None
            e["diff_leader_laps"] = None
        # Under a purple flag :meth:`snapshot` sorts by ``total_time`` whatever
        # the session mode says, so the row above is the car that crossed the
        # timing loop just before — not the car ahead on track.  Purple marks
        # formation and pace laps, where an interval carries no meaning, and a
        # blank column is honest where a plausible-looking wrong number is not.
        if purple or not entries:
            return
        values: list[float | None]
        laps: list[int | None]
        if self._is_qualifying:
            # ``$H`` carries no cumulative time, so both columns derive from
            # best laps, and no lap deficit applies.
            values = [
                _interval_seconds(e.get("best_lap_time", "")) for e in entries
            ]
            laps = [None] * len(entries)
        else:
            values = [self._time_behind_leader(e) for e in entries]
            laps = [e.get("timed_lap") for e in entries]
        # Normalise against the first-placed entry rather than assuming its own
        # value is zero: early in a session the feed's positions can briefly
        # disagree with the on-road order.  With no reference point at all,
        # neither column is defined and both stay *None*.
        base = values[0]
        if base is None:
            return
        diffs = [None if v is None else v - base for v in values]
        leader_max_laps = max((n for n in laps if n is not None), default=None)
        for i in range(1, len(entries)):
            e = entries[i]
            own_laps = laps[i]
            ahead_laps = laps[i - 1]
            if (
                leader_max_laps is not None
                and own_laps is not None
                and leader_max_laps - own_laps >= 2
            ):
                e["diff_leader_laps"] = leader_max_laps - own_laps
            elif diffs[i] is not None:
                e["diff_leader_seconds"] = round(diffs[i], 3)
            if (
                ahead_laps is not None
                and own_laps is not None
                and ahead_laps - own_laps >= 2
            ):
                e["gap_ahead_laps"] = ahead_laps - own_laps
            elif diffs[i] is not None and diffs[i - 1] is not None:
                e["gap_ahead_seconds"] = round(diffs[i] - diffs[i - 1], 3)

    def _to_dict(self) -> dict:
        """Serialise state for a :class:`~server.state_store.StateStore`."""
        return {
            "competitors": self.competitors,
            "classes": self.classes,
            "leader_time_at_lap": self.leader_time_at_lap,
            "track_name": self.track_name,
            "track_length_miles": self.track_length_miles,
            "run_description": self.run_description,
            "flag": self.flag,
            "race_time": self.race_time,
            "time_of_day": self.time_of_day,
            "time_to_go": self.time_to_go,
            "laps_to_go": self.laps_to_go,
            "_is_qualifying": self._is_qualifying,
            "_seen_race_info": self._seen_race_info,
            "last_updated": self.last_updated,
        }

    def _load_dict(self, data: dict) -> None:
        """Restore state from a dict returned by a StateStore.

        ``leader_time_at_lap`` is rebuilt with explicit ``int`` keys because
        :class:`~server.state_store.JsonFileStateStore` round-trips through
        ``json``, whose object keys are always strings.  A restored
        ``{"13": 873.95}`` would miss every lookup in
        :meth:`_time_behind_leader`, and both derived columns would go silently
        blank after a restart until the feed re-populated the index.  A
        hand-edited or truncated store degrades to an empty index rather than
        failing startup.
        """
        self.competitors = data.get("competitors", {})
        self.classes = data.get("classes", {})
        try:
            self.leader_time_at_lap = {
                int(k): float(v)
                for k, v in (data.get("leader_time_at_lap") or {}).items()
            }
        except (AttributeError, TypeError, ValueError):
            self.leader_time_at_lap = {}
        self.track_name = data.get("track_name", "")
        self.track_length_miles = data.get("track_length_miles")
        self.run_description = data.get("run_description", "")
        self.flag = data.get("flag", "")
        self.race_time = data.get("race_time", "")
        self.time_of_day = data.get("time_of_day", "")
        self.time_to_go = data.get("time_to_go", "")
        self.laps_to_go = data.get("laps_to_go", "")
        self._is_qualifying = data.get("_is_qualifying", False)
        self._seen_race_info = data.get("_seen_race_info", False)
        self.last_updated = data.get("last_updated", time.time())
        self._dirty = True

    def _derive_session_mode(self) -> str:
        """Derive a short session mode label from the run description.

        Session detection runs on two independent signals, and they are not
        interchangeable.  ``_is_qualifying`` is set by *message type* — True on a
        ``$H`` arriving before any ``$G``, cleared by any ``$G`` — and is what
        ``snapshot`` sorts on; it takes priority here, returning ``"Qualifying"``
        without reading the description at all.  The label below is derived
        separately, by substring match on ``run_description`` (from ``$B``).

        Matching is spelling-sensitive and the fallthrough is silent: a
        description matching no keyword is reported as ``"Race"``, so "Free
        Practice" and "Shakedown" both read as a race today.  Extend the keyword
        lists when a new session type appears rather than leaning on that.
        """
        if self._is_qualifying:
            return "Qualifying"
        desc = self.run_description.lower()
        if "practice" in desc or "prac" in desc or "warm" in desc or "familiarisation" in desc:
            return "Practice"
        if "qual" in desc:
            return "Qualifying"
        if self._seen_race_info or self.run_description:
            return "Race"
        return ""


def _update_position(competitor: dict, new_position: str) -> None:
    """Update position and track direction of change."""
    old = competitor["position"]
    competitor["position"] = new_position
    if old and new_position:
        try:
            old_int = int(old)
            new_int = int(new_position)
            if new_int < old_int:
                competitor["position_change"] = "up"
            elif new_int > old_int:
                competitor["position_change"] = "down"
            # If equal, keep the existing change indicator
        except (ValueError, TypeError):
            pass
    competitor["prev_position"] = old


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
        "prev_position": "",
        "position_change": "",
        "laps": "",
        "total_time": "",
        "last_lap_time": "",
        "last_lap_speed_mph": None,
        "best_lap_time": "",
        "best_lap": "",
        # Derived in RaceState._apply_intervals during snapshot(); each field
        # names its own reference because this change ships two kinds of gap.
        "gap_ahead_seconds": None,
        "gap_ahead_laps": None,
        "diff_leader_seconds": None,
        "diff_leader_laps": None,
        # Stamped as a pair by RaceState._race_info, the only handler whose
        # message carries a lap number and a cumulative time together; read as
        # a pair by _time_behind_leader and _apply_intervals.  Either alone is
        # meaningless, which is why they are named and commented as one thing.
        "timed_lap": None,
        "timed_lap_seconds": None,
    }


def _sort_key(c: dict):
    """Sort competitors by position (numeric).

    When a competitor has no position yet (opening lap) their order is
    determined by ``total_time`` – i.e. the order in which they crossed the
    timing line.  Competitors that have not yet crossed are sorted last by
    car number.

    Tuple structure: (tier, position, time_seconds, car_number)
      tier 0 – competitor has an assigned position
      tier 1 – no position but has crossed the line (sort by total_time)
      tier 2 – not yet crossed the line (sort by car number)
    """
    try:
        pos = int(c["position"])
        if pos > 0:
            return (0, pos, 0.0, "")
    except (ValueError, TypeError):
        pass
    total_time_seconds = _lap_time_seconds(c.get("total_time", ""))
    if total_time_seconds is not None and total_time_seconds > 0:
        return (1, 0, total_time_seconds, "")
    return (2, 0, 0.0, c.get("number", ""))


def _sort_key_best_lap(c: dict):
    """Sort competitors by best lap time (ascending), unknowns last.

    A zero or missing best lap time means the competitor has not set a
    timed lap yet; those entries are sorted last by car number.
    """
    return _sort_key_by_time(c, "best_lap_time")


def _sort_key_purple(c: dict):
    """Sort competitors by total_time (ascending) under a purple flag.

    When the purple flag is shown, cars are coming out onto the circuit.
    Those that have already crossed the timing loop (non-zero total_time)
    are sorted by the time at which they crossed, earliest first.
    Cars that have not yet crossed (zero or missing total_time) are sorted
    last by car number.
    """
    return _sort_key_by_time(c, "total_time")


def _sort_key_by_time(c: dict, time_field: str):
    """Common sort helper: sort by *time_field* ascending, unknowns last."""
    t = _lap_time_seconds(c.get(time_field, ""))
    if t is not None and t > 0:
        return (0, t, "")
    return (1, float("inf"), c.get("number", ""))
