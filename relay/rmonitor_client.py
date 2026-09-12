"""Async TCP client for consuming an rMonitor timing feed.

Implements the AMB RMonitor Timing Protocol and IMSA Enhanced RMon protocol
as used by MyLaps Orbits software.  Compatible with the protocol as parsed by:
  - https://github.com/only-entertainment/rmonitor
  - https://github.com/zacharyfox/RMonitorLeaderboard
"""

import asyncio
import logging

log = logging.getLogger(__name__)


class RMonitorClient:
    """Connect to an rMonitor TCP feed, parse messages, and call back.

    Reconnects on a hard connection error, and also if the feed goes
    silent for `read_timeout` seconds — a stalled/half-open TCP connection
    (e.g. dropped by a NAT or firewall idle timeout) leaves `readline()`
    blocked forever with no exception raised, so silence has to be treated
    as a failure in its own right rather than relying on the socket to
    error out.
    """

    def __init__(
        self,
        host: str,
        port: int,
        on_message,
        read_timeout: float = 30.0,
        *,
        on_connect=None,
        on_disconnect=None,
        on_raw_line=None,
    ):
        self.host = host
        self.port = port
        self.on_message = on_message
        self.read_timeout = read_timeout
        self.on_connect = on_connect if on_connect is not None else lambda: None
        self.on_disconnect = on_disconnect if on_disconnect is not None else lambda: None
        self.on_raw_line = on_raw_line if on_raw_line is not None else lambda: None
        self._reader = None
        self._writer = None

    async def run(self, reconnect_delay: float = 5.0):
        """Connect and read lines forever, reconnecting on failure."""
        while True:
            try:
                log.info("Connecting to rMonitor feed at %s:%s", self.host, self.port)
                self._reader, self._writer = await asyncio.open_connection(
                    self.host, self.port
                )
                log.info("Connected to rMonitor feed")
                self.on_connect()
                await self._read_loop()
            except (ConnectionError, OSError) as exc:
                log.warning("Connection error: %s – retrying in %ss", exc, reconnect_delay)
            except asyncio.CancelledError:
                break
            finally:
                self.on_disconnect()
                await self._close()
            await asyncio.sleep(reconnect_delay)

    async def _read_loop(self):
        while True:
            try:
                raw = await asyncio.wait_for(
                    self._reader.readline(), timeout=self.read_timeout
                )
            except asyncio.TimeoutError:
                log.warning(
                    "No data received from feed for %.0fs – treating connection "
                    "as dead and reconnecting",
                    self.read_timeout,
                )
                return
            if not raw:
                log.warning("Connection closed by remote end")
                return
            self.on_raw_line()
            line = raw.decode("utf-8", errors="replace").strip("\r\n")
            if not line:
                continue
            try:
                msg = parse_line(line)
                if msg:
                    await self.on_message(msg)
            except Exception:
                log.exception("Error processing line: %s", line)

    async def _close(self):
        if self._writer and not self._writer.is_closing():
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._writer = None
        self._reader = None


# ---------------------------------------------------------------------------
# Protocol parsing
# ---------------------------------------------------------------------------
# Both reference implementations (only-entertainment/rmonitor and
# zacharyfox/RMonitorLeaderboard) split on commas and strip double-quotes
# from each token.  We do the same here.
# ---------------------------------------------------------------------------

def _tokenize(line: str) -> list[str]:
    """Split a protocol line on commas, stripping quotes and whitespace.

    Both are load-bearing: every double-quote is removed from every token, and
    the ``strip()`` is what turns a ``$F`` flag of ``"Green "`` — real output
    from some setups — into ``"Green"``.
    """
    return [tok.replace('"', "").strip() for tok in line.split(",")]


# Supported message types – matches the MESSAGE_TYPES maps from both
# reference projects including the undocumented $SP/$SR messages.
_PARSERS: dict = {}


def parse_line(line: str) -> dict | None:
    """Parse one rMonitor protocol line into a typed dict, or *None*."""
    tokens = _tokenize(line)
    if not tokens:
        return None
    parser = _PARSERS.get(tokens[0])
    if parser is None:
        return None
    try:
        return parser(tokens)
    except (IndexError, ValueError) as exc:
        log.debug("Could not parse %s: %s", line, exc)
        return None


def _reg(cmd: str):
    """Decorator that registers a parser for *cmd*."""
    def decorator(fn):
        _PARSERS[cmd] = fn
        return fn
    return decorator


# -- $F  Heartbeat ---------------------------------------------------------
# $F,laps_to_go,"time_to_go","time_of_day","race_time","flag_status"
@_reg("$F")
def _parse_heartbeat(t: list[str]) -> dict:
    """Parse a ``$F`` line into a ``heartbeat`` dict.

    The flag is a **fixed-width six-character field**, space-padded — every one
    of the 24,273 ``$F`` records in the committed samples is exactly six
    characters, across all five values they carry: ``"Green "``, ``"Yellow"``,
    ``"Finish"``, ``"Red   "`` and six spaces for "no flag".  :func:`_tokenize`
    strips the padding, so a caller sees ``"Green"``.

    Fixed width, not merely padded, is the load-bearing distinction: a flag
    name longer than six characters is **truncated** rather than padded, so a
    comparison against a full name over six characters can never match.  Blank
    is also ambiguous — it covers pre-session, formation lap, between sessions
    and post-finish alike, so no session state can be inferred from it.
    """
    return {
        "type": "heartbeat",
        "laps_to_go": t[1],
        "time_to_go": t[2],
        "time_of_day": t[3],
        "race_time": t[4],
        "flag": t[5] if len(t) > 5 else "",
    }


# -- $A  Competitor information --------------------------------------------
# $A,"reg_number","number",transponder,"first_name","last_name",
#    "nationality",class_number
@_reg("$A")
def _parse_competitor(t: list[str]) -> dict:
    """Parse an ``$A`` line into a ``competitor`` dict.

    ``transponder`` is **not always numeric** — ``"NE2"`` and ``"NE4"``, club
    hire units, are real values from a live feed alongside ordinary numeric
    ones — so it stays a string here and anything coercing it to ``int`` will
    raise.  It is not a stable identifier either: transponders are reused
    between drivers from meeting to meeting.
    """
    return {
        "type": "competitor",
        "reg_number": t[1],
        "number": t[2],
        "transponder": t[3],
        "first_name": t[4],
        "last_name": t[5],
        "nationality": t[6] if len(t) > 6 else "",
        "class_number": t[7] if len(t) > 7 else "",
    }


# -- $COMP  Extended competitor information ---------------------------------
# $COMP,"reg_number","number",class_number,"first_name","last_name",
#       "nationality","additional_data"
@_reg("$COMP")
def _parse_comp(t: list[str]) -> dict:
    """Parse a ``$COMP`` line into a ``competitor`` dict.

    ``nationality`` and ``additional_data`` are operator-defined free text, not
    the fields their names promise: one live feed carries the car model
    (``"Radical SR10 XXR"``) and engine capacity (``"2261"``) in them, while the
    reference feeds in ``examples/`` carry a team name.  Never key behaviour off
    either.

    Nothing in this record is a stable cross-meeting identifier either: car
    numbers are reassigned and names vary in spelling between sessions, so only
    ``reg_number``, and only *within one session*, is dependable.
    """
    return {
        "type": "competitor",
        "reg_number": t[1],
        "number": t[2],
        "class_number": t[3] if len(t) > 3 else "",
        "first_name": t[4] if len(t) > 4 else "",
        "last_name": t[5] if len(t) > 5 else "",
        "nationality": t[6] if len(t) > 6 else "",
        "additional_data": t[7] if len(t) > 7 else "",
    }


# -- $B  Run information ---------------------------------------------------
# $B,unique_number,"description"
@_reg("$B")
def _parse_run(t: list[str]) -> dict:
    """Parse a ``$B`` line into a ``run`` dict.

    ``unique_number`` **95 is a session-end sentinel**, not a run: a closing
    ``$B,95`` carries the *outgoing* session's description, and a feed joined
    between sessions opens with one.  A capture cut off mid-session has none,
    so its absence means nothing.  Real run numbers vary per session and are
    re-sent throughout the session they name — ``$B,27`` arrives five times
    across one observed race, a Sebring session's record 264 times — and 95
    itself recurs, so a boundary is the ``unique_number`` *changing*, never a
    record arriving.  95 is still the only session-end signal this protocol
    offers.  ``$I`` is not an alternative: it is emitted inconsistently — none
    at all on one scoreboard reset, one after a finished race, three at a
    session start — and never at a session's end.  See
    :meth:`server.race_state.RaceState._init`.
    """
    return {
        "type": "run",
        "unique_number": t[1],
        "description": t[2] if len(t) > 2 else "",
    }


# -- $C  Class information -------------------------------------------------
# $C,unique_number,"description"
@_reg("$C")
def _parse_class(t: list[str]) -> dict:
    return {
        "type": "class_info",
        "unique_number": t[1],
        "description": t[2] if len(t) > 2 else "",
    }


# -- $E  Setting information -----------------------------------------------
# $E,"description","value"
@_reg("$E")
def _parse_setting(t: list[str]) -> dict:
    return {
        "type": "setting",
        "description": t[1],
        "value": t[2] if len(t) > 2 else "",
    }


# -- $G  Race / position information ----------------------------------------
# $G,position,"reg_number",laps,"total_time"
@_reg("$G")
def _parse_race_info(t: list[str]) -> dict:
    return {
        "type": "race_info",
        "position": t[1],
        "reg_number": t[2],
        "laps": t[3] if len(t) > 3 else "",
        "total_time": t[4] if len(t) > 4 else "",
    }


# -- $H  Practice / qualifying information ----------------------------------
# $H,position,"reg_number",best_lap,"best_lap_time"
@_reg("$H")
def _parse_qual_info(t: list[str]) -> dict:
    return {
        "type": "qual_info",
        "position": t[1],
        "reg_number": t[2],
        "best_lap": t[3] if len(t) > 3 else "",
        "best_lap_time": t[4] if len(t) > 4 else "",
    }


# -- $I  Init record (new session) -----------------------------------------
# $I,"time_of_day","date"
@_reg("$I")
def _parse_init(t: list[str]) -> dict:
    return {
        "type": "init",
        "time_of_day": t[1] if len(t) > 1 else "",
        "date": t[2] if len(t) > 2 else "",
    }


# -- $J  Passing information -----------------------------------------------
# $J,"reg_number","lap_time","total_time"
@_reg("$J")
def _parse_passing(t: list[str]) -> dict:
    return {
        "type": "passing",
        "reg_number": t[1],
        "lap_time": t[2] if len(t) > 2 else "",
        "total_time": t[3] if len(t) > 3 else "",
    }


# -- $SP/$SR  Lap information (undocumented) --------------------------------
# $SP/$SR,position,"reg_number",lap_number,"lap_time"
def _parse_lap_info(t: list[str]) -> dict:
    """Parse a ``$SP``/``$SR`` line into a ``lap_info`` dict.

    Neither message appears in the protocol documentation; both are real output
    from some Orbits setups, carrying per-lap position and time.
    """
    return {
        "type": "lap_info",
        "position": t[1],
        "reg_number": t[2],
        "lap_number": t[3] if len(t) > 3 else "",
        "lap_time": t[4] if len(t) > 4 else "",
    }

_PARSERS["$SP"] = _parse_lap_info
_PARSERS["$SR"] = _parse_lap_info
