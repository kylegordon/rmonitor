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
from pathlib import Path

from aiohttp import web

from server.race_state import RaceState
from server.server import BROADCAST_INTERVAL, broadcast, create_app
from server.state_store import JsonFileStateStore

log = logging.getLogger("server")

WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/state.json"))
SAVE_INTERVAL = float(os.environ.get("SAVE_INTERVAL", "10"))
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")

store = JsonFileStateStore(STATE_FILE)
race_state = RaceState()
saved = store.load()
if saved:
    race_state._load_dict(saved)

app = create_app(race_state, relay_secret=RELAY_SECRET)


async def _broadcast_loop() -> None:
    """Periodically push dirty state to WebSocket clients."""
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        if race_state.dirty:
            await broadcast(app, "update", race_state.snapshot())
            race_state.mark_clean()


async def _save_loop() -> None:
    """Periodically persist race state to the state store."""
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        try:
            store.save(race_state._to_dict())
        except OSError as exc:
            log.warning("Failed to save state: %s", exc)


async def start_background_tasks(_app: web.Application) -> None:
    _app["broadcast_task"] = asyncio.create_task(_broadcast_loop())
    _app["save_task"] = asyncio.create_task(_save_loop())


async def cleanup_background_tasks(_app: web.Application) -> None:
    try:
        store.save(race_state._to_dict())
    except OSError as exc:
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
    log.info("Starting server – web=%s:%s", WEB_HOST, WEB_PORT)
    web.run_app(app, host=WEB_HOST, port=WEB_PORT)
