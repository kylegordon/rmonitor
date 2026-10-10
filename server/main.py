"""Entry point for the web server.

Receives rMonitor messages via POST /api/ingest (sent by the relay),
maintains race state, and pushes live updates to browser clients over
WebSocket.

There is no TCP connection to the timing system here; that is the relay's
responsibility.
"""

import asyncio
import logging
import os
import threading
from pathlib import Path

from aiohttp import web

from server.race_state import RaceState
from server.server import (
    BROADCAST_INTERVAL,
    RELEASE_VERSION,
    broadcast_if_dirty,
    create_app,
)
from server.state_store import JsonFileStateStore

log = logging.getLogger("server")

WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/state.json"))
SAVE_INTERVAL = float(os.environ.get("SAVE_INTERVAL", "10"))
STATE_MAX_AGE = float(os.environ.get("STATE_MAX_AGE", str(15 * 60)))
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")

store = JsonFileStateStore(STATE_FILE, max_age_seconds=STATE_MAX_AGE)
# The class-code registry is stored beside the race state with no whole-file
# age cutoff: every entry carries its own expiry (RaceState.class_codes_to_dict).
class_codes_store = JsonFileStateStore(
    STATE_FILE.with_name(STATE_FILE.stem + "-class-codes.json")
)
race_state = RaceState()
saved = store.load()
if saved:
    race_state._load_dict(saved)
race_state.load_class_codes(class_codes_store.load())


async def _broadcast_loop() -> None:
    """Periodically push dirty state to WebSocket clients."""
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        try:
            await broadcast_if_dirty(app)
        except Exception:
            log.exception("Broadcast loop iteration failed")


_saved_class_codes_revision: int | None = None
# Serialises every write: JsonFileStateStore writes a fixed <path>.tmp and is
# not safe for concurrent writers, and the periodic save runs in a worker
# thread while an ingest's immediate save (_save_class_codes) runs in another.
_save_lock = threading.Lock()


def _save_all() -> None:
    global _saved_class_codes_revision
    with _save_lock:
        store.save(race_state._to_dict())
        # Rewritten only when it changed: with a registry preload it is some
        # hundreds of KB, and this runs every SAVE_INTERVAL.  The revision is
        # read first, so a change landing mid-save is saved next time.
        revision = race_state.class_codes_revision
        if revision != _saved_class_codes_revision:
            class_codes_store.save(race_state.class_codes_to_dict())
            _saved_class_codes_revision = revision


def _write_class_codes(data: dict, revision: int) -> None:
    """Write *data*, the class-code store as of *revision*, unless already newer.

    The revision only ever increases, so a store saved at or past *revision*
    (by the periodic save, which may have run while *data* waited for the
    lock) is at least as new, and overwriting it would lose a change.
    """
    global _saved_class_codes_revision
    with _save_lock:
        if (
            _saved_class_codes_revision is not None
            and _saved_class_codes_revision >= revision
        ):
            return
        class_codes_store.save(data)
        _saved_class_codes_revision = revision


async def _save_class_codes() -> None:
    """Persist the class-code store now, for a run start about to be acknowledged.

    Writes nothing when the store on disk is already current, so a repeated
    or rejected start costs one comparison.  The dict is built here, on the
    loop, so no ingest can change it mid-build; only the write goes to a
    thread.  Exceptions propagate: the caller answers them with a 503.
    """
    revision = race_state.class_codes_revision
    if (
        _saved_class_codes_revision is not None
        and _saved_class_codes_revision >= revision
    ):
        return
    data = race_state.class_codes_to_dict()
    await asyncio.to_thread(_write_class_codes, data, revision)


app = create_app(
    race_state,
    relay_secret=RELAY_SECRET,
    restored=bool(saved),
    save_class_codes=_save_class_codes,
)


async def _save_loop() -> None:
    """Periodically persist race state and the class-code registry."""
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        try:
            await asyncio.to_thread(_save_all)
        except Exception as exc:
            log.warning("Failed to save state: %s", exc)


async def start_background_tasks(_app: web.Application) -> None:
    _app["broadcast_task"] = asyncio.create_task(_broadcast_loop())
    _app["save_task"] = asyncio.create_task(_save_loop())


async def cleanup_background_tasks(_app: web.Application) -> None:
    try:
        await asyncio.to_thread(_save_all)
    except Exception as exc:
        log.warning("Failed to save state on shutdown: %s", exc)
    for key in ("broadcast_task", "save_task"):
        task = _app.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)


def main() -> None:
    if not RELAY_SECRET:
        log.warning(
            "RELAY_SECRET is not set – /api/ingest is unauthenticated"
        )
    log.info("Starting server %s – web=%s:%s", RELEASE_VERSION, WEB_HOST, WEB_PORT)
    web.run_app(app, host=WEB_HOST, port=WEB_PORT)
