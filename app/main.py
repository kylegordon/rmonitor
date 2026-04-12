#!/usr/bin/env python3
"""Entry point – starts the rMonitor TCP client and the web server."""

import asyncio
import logging
import os

from aiohttp import web

from app.race_state import RaceState
from app.rmonitor_client import RMonitorClient
from app.server import broadcast, create_app

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

# Shared state
race_state = RaceState()
app = create_app(race_state)


async def on_message(msg: dict):
    """Called by the rMonitor client for every parsed message."""
    event = race_state.process(msg)
    if event == "init":
        # New session/race – push full reset to all clients
        await broadcast(app, "init", race_state.snapshot())
    elif event and race_state.dirty:
        await broadcast(app, "update", race_state.snapshot())
        race_state.mark_clean()


async def start_background_tasks(_app: web.Application):
    client = RMonitorClient(RMONITOR_HOST, RMONITOR_PORT, on_message)
    _app["rmonitor_task"] = asyncio.create_task(client.run())


async def cleanup_background_tasks(_app: web.Application):
    task = _app.get("rmonitor_task")
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
