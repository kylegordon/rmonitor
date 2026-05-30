#!/usr/bin/env python3
"""Entry point – starts the rMonitor TCP client and the web server."""

import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from app.race_state import RaceState
from app.rmonitor_client import RMonitorClient
from app.server import BROADCAST_INTERVAL, broadcast, create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rmonitor")

# Configuration via environment variables
RMONITOR_HOST = os.environ.get("RMONITOR_HOST", "127.0.0.1")
RMONITOR_PORT = int(os.environ.get("RMONITOR_PORT", "50000"))
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "data/state.json"))
SAVE_INTERVAL = float(os.environ.get("SAVE_INTERVAL", "10"))  # seconds

# Shared state
race_state = RaceState()
race_state.load(STATE_FILE)
app = create_app(race_state)


async def on_message(msg: dict):
    """Called by the rMonitor client for every parsed message."""
    event = race_state.process(msg)
    if event == "init":
        # New session/race – push full reset to all clients immediately
        await broadcast(app, "init", race_state.snapshot())
        race_state.mark_clean()


async def _broadcast_loop():
    """Periodically push dirty state to WebSocket clients.

    This decouples the message-processing rate from the broadcast rate so
    that bursts of rMonitor messages (e.g. many cars crossing the line)
    are coalesced into a single UI update.
    """
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL)
        if race_state.dirty:
            await broadcast(app, "update", race_state.snapshot())
            race_state.mark_clean()


async def _save_loop():
    """Periodically persist race state to disk."""
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        try:
            race_state.save(STATE_FILE)
        except OSError as exc:
            log.warning("Failed to save state: %s", exc)


async def start_background_tasks(_app: web.Application):
    client = RMonitorClient(RMONITOR_HOST, RMONITOR_PORT, on_message)
    _app["rmonitor_task"] = asyncio.create_task(client.run())
    _app["broadcast_task"] = asyncio.create_task(_broadcast_loop())
    _app["save_task"] = asyncio.create_task(_save_loop())


async def cleanup_background_tasks(_app: web.Application):
    # Save state one final time on shutdown
    try:
        race_state.save(STATE_FILE)
    except OSError as exc:
        log.warning("Failed to save state on shutdown: %s", exc)
    for key in ("rmonitor_task", "broadcast_task", "save_task"):
        task = _app.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == "__main__":
    log.info(
        "Starting rMonitor web display – feed=%s:%s  web=%s:%s",
        RMONITOR_HOST, RMONITOR_PORT, WEB_HOST, WEB_PORT,
    )
    web.run_app(app, host=WEB_HOST, port=WEB_PORT)
