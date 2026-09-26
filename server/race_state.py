"""In-memory race state built from rMonitor messages.

State is populated by the parsed message dicts produced by
``rmonitor_client.parse_line``.  Field names follow the AMB RMonitor Timing
Protocol as implemented by:
  - https://github.com/only-entertainment/rmonitor
  - https://github.com/zacharyfox/RMonitorLeaderboard
"""

from __future__ import annotations

import logging
import math
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

# Substrings of a lowercased ``$B`` run description that mark a non-competitive
# session.  Any one match wins, so their order here carries no meaning; what
# does is that :meth:`RaceState._derive_session_mode` tests them *before* the
# ``"qual"`` test, so a description matching both reads as practice.
_PRACTICE_KEYWORDS = (
    "practice",
    "prac",
    "warm",
    "familiarisation",
    "familiarization",
    "test",
    "shakedown",
    "sighting",
    "untimed",
)

# How long a pushed class code stays joinable after its last push.  Twelve
# hours covers a meeting day, including a server restart; transponders are
# reused across meetings, so a longer life would pair a reused transponder
# with last meeting's code — a plausible wrong value, never shown.
_CLASS_CODE_TTL_SECONDS = 12 * 3600

# The registry fields a ``class_codes`` entry carries, all strings.
_CLASS_CODE_FIELDS = ("entrant_id", "number", "class_name", "transponder", "class_code")

# The fields a ``class_code_preload`` entry carries, all strings.
_PRELOAD_FIELDS = ("transponder", "class_name", "class_code")


def _interval_seconds(value: str) -> float | None:
    """Convert a feed time field to seconds, or *None* if unusable.

    The single input guard for both session modes.  In a race
    :meth:`RaceState._race_info` passes a cumulative ``total_time``; on the
    best-lap branch — practice and qualifying alike —
    :meth:`RaceState._apply_intervals` passes a ``best_lap_time``.  Both want
    the same four checks and differ only in which field they read, so neither
    branch may reach the interval derivation by another route.

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
        # Keyed ``"<run id>\t<entrant id>"``; see _class_codes.  Created here,
        # not in reset(), because reset() must never clear it.
        self.class_codes: dict[str, dict] = {}
        # The last registry preload; see _class_code_preload.  Never cleared
        # by reset(), for the same reason.
        self.class_code_preload: dict = _preload_store([], None)
        self.reset()

    def reset(self):
        # self.class_codes is deliberately left alone: pushes follow entry-list
        # edits and are not repeated, so an entrant's code may have arrived
        # long before the $I burst that starts its session.  Clearing it here
        # would lose codes the server never gets again.  The preload is kept
        # for the same reason: the relay pulls it again only when it reconnects.
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
            # last_updated dates the *race* state, and the store discards it by
            # that age; class codes come from another source and live in a
            # store of their own, so they must not make stale race state fresh.
            if event is not None and event != "class_codes":
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
        if msg.get("transponder"):
            c["transponder"] = msg["transponder"]
        if msg.get("class_number"):
            c["class_number"] = msg["class_number"]
        if msg.get("additional_data"):
            c["additional_data"] = msg["additional_data"]
        self._dirty = True
        return "competitor"

    def _run(self, msg: dict) -> str:
        """Record the run description from ``$B``.

        ``unique_number`` is deliberately dropped: nothing here consumes it
        yet.  It is not noise, though — 95 marks a session's end (see
        :func:`relay.rmonitor_client._parse_run`), so this is where a reliable
        session-boundary signal would be picked up if one is ever needed.
        """
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
        """Clear all state in response to ``$I``.

        ``$I`` is not the session marker its name suggests, and is emitted
        inconsistently: a live feed sent none at all on a scoreboard reset, one
        after a finished race, and **three within two milliseconds** at a
        session start, followed by a duplicated ``$B``.  It never appears at a
        session's *end*, where ``$B,95`` does — the two are asymmetric, and
        neither brackets a session alone.

        So treat ``$I`` as a wipe to survive rather than an edge to act on: it
        is not idempotent in practice, and each one costs a full reset plus a
        broadcast to every client.  What makes that safe is only ordering — the
        repopulating records follow in the same batch.  Derive session
        boundaries from ``$B`` instead — but from the **change** in its
        ``unique_number``, never from a record's arrival: a live session
        re-sends its own run record throughout (``$B,27`` five times across one
        race, a Sebring session's 264 times) and 95 recurs too, so acting on
        every one would reopen a session already running.  Becoming 95 is the
        end edge; becoming any other number is the start edge.
        """
        log.info("New race/session – clearing all state")
        self.reset()
        return "init"

    def _class_codes(self, msg: dict) -> str | None:
        """Store the class codes the relay read from the timing host's ``:51738``.

        The registry is keyed by run id and entrant id, last push wins, and it
        accumulates across runs: pushes cover runs other than the one on the
        rMonitor feed, and :meth:`_resolve_class_codes` gates both of its
        layers on an exact class-name match.  It survives :meth:`reset` for the
        reason given there, and expires by :data:`_CLASS_CODE_TTL_SECONDS`
        instead.

        ``entries`` is untrusted: :func:`_coerce_scalars` leaves lists alone,
        so every item is checked and coerced here, and one without an entrant
        id or a code is skipped.

        Each entry is dated from its push, not from its arrival: the relay
        retries an undelivered batch, and a retry landing hours later must not
        earn a fresh TTL.  So ``received_at`` is now minus the entry's
        ``age_seconds`` — a duration on the relay's own clock, so the two
        hosts' clocks need not agree — and an entry already past the TTL is
        not stored.  A missing or unusable age counts as zero.
        """
        entries = msg.get("entries")
        run_id = msg.get("run_id") or ""
        now = time.time()
        changed = False
        stored = 0
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                entry = {
                    k: str(item[k]) if item.get(k) is not None else ""
                    for k in _CLASS_CODE_FIELDS
                }
                if not entry["entrant_id"] or not entry["class_code"]:
                    continue
                age = _entry_age(item.get("age_seconds"))
                if age > _CLASS_CODE_TTL_SECONDS:
                    continue
                entry["received_at"] = now - age
                self.class_codes[f"{run_id}\t{entry.pop('entrant_id')}"] = entry
                changed = True
                stored += 1
        # Logged whatever was stored: a batch of codeless records stores
        # nothing, and is otherwise indistinguishable from no batch at all.
        log.info(
            "Class codes for run %s: stored %d of %d entries",
            run_id, stored, len(entries) if isinstance(entries, list) else 0,
        )
        changed = self._prune_class_codes(now) or changed
        if not changed:
            return None
        self._dirty = True
        return "class_codes"

    def _class_code_preload(self, msg: dict) -> str | None:
        """Store the competitor registry the relay pulled from ``:51738``.

        Each pull is a full snapshot, so it replaces the previous preload
        whole rather than accumulating.  It is kept apart from the pushed
        registry so a push always overrides it, and only
        :meth:`_resolve_class_codes`'s third layer reads it.  Like the pushed
        registry it survives :meth:`reset` and expires by
        :data:`_CLASS_CODE_TTL_SECONDS`.

        ``entries`` is untrusted and coerced as in :meth:`_class_codes`; an item
        missing any field, or with transponder ``""`` or ``"0"``, is skipped,
        and a preload with nothing usable leaves the previous one in place.
        The store is dated from the pull — now minus the message's
        ``age_seconds`` — and a message already past the TTL is ignored.
        """
        entries = msg.get("entries")
        if not isinstance(entries, list):
            entries = []
        usable = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            entry = {
                k: str(item[k]) if item.get(k) is not None else "" for k in _PRELOAD_FIELDS
            }
            if not all(entry.values()) or entry["transponder"] == "0":
                continue
            usable.append(entry)
        log.info("Class-code preload: %d usable of %d records", len(usable), len(entries))
        age = _entry_age(msg.get("age_seconds"))
        if not usable or age > _CLASS_CODE_TTL_SECONDS:
            return None
        self.class_code_preload = _preload_store(usable, time.time() - age)
        self._dirty = True
        return "class_codes"

    def _prune_class_code_preload(self, now: float) -> bool:
        """Drop the preload once past its TTL; return whether it went."""
        stamp = self.class_code_preload["received_at"]
        if stamp is None or now - stamp <= _CLASS_CODE_TTL_SECONDS:
            return False
        self.class_code_preload = _preload_store([], None)
        return True

    def _prune_class_codes(self, now: float) -> bool:
        """Drop registry entries past their TTL; return whether any went."""
        expired = [
            k for k, v in self.class_codes.items()
            if now - v["received_at"] > _CLASS_CODE_TTL_SECONDS
        ]
        for k in expired:
            del self.class_codes[k]
        return bool(expired)

    def _resolve_class_codes(self, entries: list[dict]) -> int:
        """Write ``class_code`` onto every entry; return how many have none.

        Three layers, each failing to blank — never to a guess, and each gated
        on the record's class name equalling ``class_description`` exactly and
        non-empty.  A prefix would bind a ``Modsports A`` record to an ``A2``
        session; without the gate on layer 1, a driver entered in two classes
        at one meeting on one transponder would be shown the other class's
        code, since the registry accumulates across runs.

        1. **Transponder** (not ``""`` or ``"0"``) and class → the latest push
           carrying both.  First because it survives an operator's mid-session
           renumber, which reaches the push side before the rMonitor feed: seen
           twice on one day, ``231`` → ``23`` and ``190`` → ``90``, each with
           the class unchanged.
        2. **Exact** ``(number, class_description)`` → a code only when every
           matching record carries one distinct code.  Distinct codes, not
           records, because the same entrant is pushed under several run ids.
        3. **The registry preload**, only when neither push layer gives a
           code: transponder and class → a code only when that pair carries
           one distinct code *and* every preload record with that exact class
           name carries that same code.  The guard is there because the
           registry holds a competitor's *registered* code, and a meeting's
           entry can override it: measured against pushes, the pair alone was
           wrong 2 times in 24 in a class whose name spans several codes, and
           0 times with the guard.  The preload never takes part in the number
           layer — the registry spans seasons, and numbers change between them.

        Nothing ever derives a code from a class name — the mapping between
        them is many-to-many.
        """
        now = time.time()
        self._prune_class_codes(now)
        self._prune_class_code_preload(now)
        pre_tx = self.class_code_preload["by_tx"]
        pre_class = self.class_code_preload["by_class"]
        by_tx: dict[tuple[str, str], dict] = {}
        by_nc: dict[tuple[str, str], set[str]] = {}
        for rec in self.class_codes.values():
            tx, cls = rec["transponder"], rec["class_name"]
            if tx not in ("", "0"):
                best = by_tx.get((tx, cls))
                if best is None or rec["received_at"] >= best["received_at"]:
                    by_tx[(tx, cls)] = rec
            by_nc.setdefault((rec["number"], cls), set()).add(rec["class_code"])
        missing = 0
        for e in entries:
            code = ""
            tx = e.get("transponder", "")
            desc = e.get("class_description", "")
            if desc:
                if tx not in ("", "0") and (tx, desc) in by_tx:
                    code = by_tx[(tx, desc)]["class_code"]
                else:
                    codes = by_nc.get((e.get("number", ""), desc), set())
                    if len(codes) == 1:
                        code = next(iter(codes))
                if not code and tx not in ("", "0"):
                    codes = pre_tx.get((tx, desc), set())
                    if len(codes) == 1 and pre_class.get(desc) == codes:
                        code = next(iter(codes))
            e["class_code"] = code
            if not code:
                missing += 1
        return missing

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
        "class_codes": _class_codes,
        "class_code_preload": _class_code_preload,
    }

    # ---- serialisation ----

    def _sort_mode(self, session_mode: str) -> str:
        """Pick the one mode that drives the sort order *and* the intervals.

        Which order the field is shown in and which reference the ``Gap`` and
        ``Diff`` columns derive from are a single decision, not two: ``Gap``
        means "interval to the row above", so if the values switch to best laps
        while the rows stay in track order, the row above is no longer the
        next-fastest car and the column goes negative.  Replaying the captures
        measured that disagreement at 17 negative gaps in the Familiarisation
        session against 4 for the status quo — worse, not better.  Returning one
        value that both consumers read makes them structurally unable to differ.

        Keying this on ``_is_qualifying`` was the defect.  That flag is set by
        *message type* — a ``$H`` arriving before any ``$G`` — and cleared
        permanently by any ``$G`` (:meth:`_race_info`, :meth:`_qual_info`).
        Every capture and example file in this repository emits ``$G``,
        including two sessions named ``Qualifying``, so the flag measures
        ``False`` in every real session and the best-lap sort and best-lap
        interval branch were reachable only in a session's opening seconds.
        The ``session_mode`` label is the signal that survives.  It is not a
        rename of the flag: the label usually comes from ``$B``, but
        :meth:`_derive_session_mode` tests ``_is_qualifying`` first and
        unconditionally, so while that flag is set the label reads
        ``"Qualifying"`` whatever ``$B`` said and this method returns
        ``"best_lap"``.  Any ``$G`` clears the flag permanently, so that is a
        session's opening moments — or the whole of a pure-``$H`` session.
        Either way the flag reaches the sort through the label, never around it.

        The purple branch has never been reached by a real feed.  One live
        meeting ran a purple flag for a full form-up and the ``$F`` flag stayed
        *blank* for all 610 heartbeats between the previous session ending and
        the green, then went straight to ``"Green "``: that installation does
        not treat purple as a timing state.  Do not "fix" the comparison by
        guessing a different spelling — the string is absent, not misspelled,
        and ``"Purple"`` is exactly six characters so it would arrive intact if
        it were sent at all.  It is kept because the parser is shared and only
        one installation has been observed.  The tests covering it synthesise
        the heartbeat, so they prove the ordering logic, not reachability.

        :param session_mode: the label from :meth:`_derive_session_mode`.
        :returns: ``"total_time"`` under a purple flag, which overrides the
            session; ``"best_lap"`` for ``"Practice"`` and ``"Qualifying"``;
            ``"position"`` otherwise.
        """
        if str(self.flag).strip().lower() == "purple":
            return "total_time"
        if session_mode in ("Practice", "Qualifying"):
            return "best_lap"
        return "position"

    def snapshot(self) -> dict:
        """Return the full state as a JSON-serialisable dict.

        Sort order follows the session-mode label: ``best_lap_time`` ascending
        in practice and qualifying, numeric ``position`` otherwise.  A purple
        flag overrides both with ``total_time`` ascending whatever the mode says
        — intended for formation and pace laps at race end, where cars are on
        track in the order they crossed the timing loop.  :meth:`_sort_mode`
        makes that one choice, and ``"sort_mode"`` in the returned dict is the
        payload's single source of truth for which order the page is being
        shown: ``index.html`` reads it rather than re-deriving the condition
        from ``session_mode`` and ``flag``.
        """
        session_mode = self._derive_session_mode()
        sort_mode = self._sort_mode(session_mode)
        if sort_mode == "total_time":
            sort_fn = _sort_key_purple
        elif sort_mode == "best_lap":
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
        class_code_missing = self._resolve_class_codes(entries)
        self._apply_intervals(entries, sort_mode=sort_mode)
        return {
            "track_name": self.track_name,
            "track_length_miles": self.track_length_miles,
            "run_description": self.run_description,
            "session_mode": session_mode,
            "sort_mode": sort_mode,
            "flag": self.flag,
            "race_time": self.race_time,
            "time_of_day": self.time_of_day,
            "time_to_go": self.time_to_go,
            "laps_to_go": self.laps_to_go,
            "class_codes_available": bool(
                self.class_codes or self.class_code_preload["entries"]
            ),
            "class_code_missing": class_code_missing,
            "entries": entries,
        }

    def _apply_intervals(self, entries: list[dict], *, sort_mode: str) -> None:
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

        Three choices the code cannot explain:

        - **The lap-deficit threshold is 2, not 1.**  A one-lap difference is
          the *normal* state — the car ahead has crossed the line for this lap
          and you have not — so a threshold of 1 would flicker between ``+1 L``
          and a time roughly once per lap.
        - **Seconds are rounded to 3 dp**, matching the feed's millisecond
          resolution.  That is what keeps each ``Diff`` exactly equal to the
          running sum of the ``Gap`` values above it rather than float-noisy.
        - **A negative ``Gap`` is blanked, not rendered and not converted.**
          The subtraction is this row's deficit minus the row above's, and it
          goes negative whenever the row above lost more time to the leader on
          its latest lap than the real on-track gap between the two — which
          happens for a *stalled* car, whose deficit froze at its last crossing,
          and equally for a car merely inside the one-lap crossing window.
          Nothing available here separates those two, so neither a time nor a
          ``+1 L`` can be emitted honestly: replayed over the captures, a
          ``+1 L`` here fires on cars seconds apart and flips back to a time
          once a lap, which is the flicker the threshold of 2 exists to prevent.
          Both gap fields stay *None* for an em-dash, on the same reasoning as
          the purple-flag blank below.
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
        if sort_mode == "total_time" or not entries:
            return
        values: list[float | None]
        laps: list[int | None]
        if sort_mode == "best_lap":
            # The rows are in best-lap order, so the reference has to be the
            # best lap too.  Reached by practice and qualifying alike; in a
            # pure-``$H`` feed there is no cumulative time to use anyway.  No
            # lap deficit applies to a comparison of single laps.
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
                gap = round(diffs[i] - diffs[i - 1], 3)
                if gap >= 0:
                    e["gap_ahead_seconds"] = gap

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
        failing startup.  The class-code registry is not part of this dict; see
        :meth:`class_codes_to_dict`.
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

    def class_codes_to_dict(self) -> dict:
        """Serialise the class-code registry for a store of its own.

        It is kept out of :meth:`_to_dict` because the race-state store is
        discarded whole once it is older than ``STATE_MAX_AGE`` — 15 minutes —
        and a restart after a quiet gap between sessions would then lose codes
        pushed for runs not yet started, which are never pushed again.  The
        registry needs no such cutoff: every entry expires on its own
        :data:`_CLASS_CODE_TTL_SECONDS`, applied by :meth:`load_class_codes`.
        The registry preload is saved beside it under ``"preload"``, for the
        same reason and because the relay pulls it again only on a reconnect.
        """
        return {
            "class_codes": self.class_codes,
            "preload": {
                "received_at": self.class_code_preload["received_at"],
                "entries": self.class_code_preload["entries"],
            },
        }

    def load_class_codes(self, data: dict) -> None:
        """Restore the registry from :meth:`class_codes_to_dict`'s output.

        A malformed store degrades to an empty registry rather than failing
        startup, and expired entries are pruned on the way in.  The preload
        is restored the same way and independently: a missing or malformed
        ``"preload"`` leaves it empty without touching the pushed registry.
        """
        # A timestamp must be finite — json accepts NaN and Infinity, and
        # neither is ever pruned — and one in the future is capped at now, so
        # a corrupt store can neither keep a code forever nor extend its life.
        now = time.time()
        try:
            self.class_codes = {
                k: {f: str(v.get(f, "")) for f in _CLASS_CODE_FIELDS[1:]}
                | {"received_at": min(stamp, now)}
                for k, v in (data.get("class_codes") or {}).items()
                if isinstance(k, str) and isinstance(v, dict)
                # The invariant _class_codes holds on ingest: never a codeless record.
                and v.get("class_code") not in (None, "")
                and (stamp := _finite_stamp(v.get("received_at"))) is not None
            }
        except (AttributeError, TypeError, ValueError):
            self.class_codes = {}
        self._prune_class_codes(now)
        self.class_code_preload = _preload_store([], None)
        try:
            preload = data.get("preload") or {}
            stamp = _finite_stamp(preload.get("received_at"))
            entries = [
                {f: str(item[f]) for f in _PRELOAD_FIELDS}
                for item in preload.get("entries") or []
                if isinstance(item, dict)
                and all(item.get(f) not in (None, "") for f in _PRELOAD_FIELDS)
                and str(item["transponder"]) != "0"
            ]
            if stamp is not None and entries:
                self.class_code_preload = _preload_store(entries, min(stamp, now))
        except (AttributeError, TypeError, ValueError):
            pass
        self._prune_class_code_preload(now)

    def _derive_session_mode(self) -> str:
        """Derive a short session mode label from the run description.

        This label is the session signal: :meth:`_sort_mode` derives the sort
        order and the interval reference from it.  ``_is_qualifying`` is set by
        *message type* — True on a ``$H`` arriving before any ``$G``, cleared by
        any ``$G`` — and is tested first and *unconditionally*: it overrides
        ``run_description`` rather than filling in for a missing one.  A feed
        that has sent ``$B,"Race 1"`` and then a ``$H`` reports ``"Qualifying"``
        until its first ``$G`` arrives, and because :meth:`_sort_mode` keys on
        this label, that window is shown in best-lap order too — a race briefly
        sorted by best lap.  Any ``$G`` clears the flag for good, so in a feed
        that sends them the window is a session's opening moments; in a
        pure-``$H`` feed it is the whole session, which is the case the test
        exists for.  Everything below it is a substring match on
        ``run_description`` (from ``$B``).

        Matching is on bare substrings and the fallthrough is silent: a
        description matching no keyword is reported as ``"Race"``.  Both halves
        of that cost are real.  ``"test"`` in :data:`_PRACTICE_KEYWORDS` also
        matches ``Contest``, ``Protest`` and ``Fastest``, so a session named for
        any of those would silently read as practice; no ``$B`` description in
        this repository's ``captures/*.log`` or ``examples/*.txt`` collides, and
        word-boundary matching was not adopted because the corpus does not
        justify it.  In the other direction, an unmatched description is not
        flagged — it is simply reported as a race.  Extend
        :data:`_PRACTICE_KEYWORDS` when a new session type appears rather than
        leaning on either.
        """
        if self._is_qualifying:
            return "Qualifying"
        desc = self.run_description.lower()
        if any(word in desc for word in _PRACTICE_KEYWORDS):
            return "Practice"
        if "qual" in desc:
            return "Qualifying"
        if self._seen_race_info or self.run_description:
            return "Race"
        return ""


def _preload_store(entries: list[dict], received_at: float | None) -> dict:
    """Return a preload store: its *entries*, their date, and the join indexes.

    ``by_tx`` maps ``(transponder, class name)`` and ``by_class`` a class name
    to the set of codes the entries carry; both are built once per store so a
    snapshot does not rebuild them.
    """
    by_tx: dict[tuple[str, str], set[str]] = {}
    by_class: dict[str, set[str]] = {}
    for e in entries:
        by_tx.setdefault((e["transponder"], e["class_name"]), set()).add(e["class_code"])
        by_class.setdefault(e["class_name"], set()).add(e["class_code"])
    return {"received_at": received_at, "entries": entries, "by_tx": by_tx, "by_class": by_class}


def _finite_stamp(value) -> float | None:
    """Return a restored ``received_at`` as a finite float, or *None*.

    ``json`` decodes ``NaN``, ``Infinity`` and integers too large for a float;
    none of them is a usable time, and the last would raise ``OverflowError``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        stamp = float(value)
    except OverflowError:
        return None
    return stamp if math.isfinite(stamp) else None


def _entry_age(value) -> float:
    """Return a class-code entry's ``age_seconds`` as a finite non-negative float.

    Anything else — absent, non-numeric, negative, NaN or infinite — reads as
    zero, which is what the entry's age was before the relay sent one.
    """
    if isinstance(value, bool):
        return 0.0
    try:
        age = float(value)
    except (TypeError, ValueError):
        return 0.0
    return age if math.isfinite(age) and age > 0 else 0.0


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
        "transponder": "",
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
