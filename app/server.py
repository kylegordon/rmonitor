"""aiohttp web server with WebSocket push for live race state."""

import asyncio
import json
import logging
import os
import pathlib
import uuid

from aiohttp import web

log = logging.getLogger(__name__)
TEMPLATES = pathlib.Path(__file__).parent / "templates"

BROADCAST_INTERVAL = float(os.environ.get("BROADCAST_INTERVAL", "0.25"))

# Typed app keys (avoids NotAppKeyWarning)
race_state_key = web.AppKey("race_state")
ws_clients_key = web.AppKey("ws_clients", set)
server_instance_id_key = web.AppKey("server_instance_id", str)


def create_app(race_state) -> web.Application:
    app = web.Application()
    app[race_state_key] = race_state
    app[ws_clients_key] = set()
    app[server_instance_id_key] = str(uuid.uuid4())

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_get("/healthz", handle_healthz)

    return app


async def handle_index(request: web.Request) -> web.Response:
    html = (TEMPLATES / "index.html").read_text()
    return web.Response(text=html, content_type="text/html")


async def handle_api_state(request: web.Request) -> web.Response:
    state = request.app[race_state_key]
    return web.json_response(state.snapshot())


async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    clients: set = request.app[ws_clients_key]
    clients.add(ws)
    state = request.app[race_state_key]
    log.info("WebSocket client connected (%d total)", len(clients))
    try:
        # Send the full current state on connect, including the server instance ID
        # so clients can detect a server restart and reload the page.
        await ws.send_json({
            "event": "full",
            "data": state.snapshot(),
            "server_instance_id": request.app[server_instance_id_key],
        })
        async for _msg in ws:
            pass  # We don't expect client-to-server messages
    finally:
        clients.discard(ws)
        log.info("WebSocket client disconnected (%d remaining)", len(clients))
    return ws


async def broadcast(app: web.Application, event: str, data: dict):
    """Send a JSON message to every connected WebSocket client.

    Sends to all clients concurrently so a single slow client cannot
    block others.  Clients that fail to receive within 5 seconds are
    removed.
    """
    payload = json.dumps({
        "event": event,
        "data": data,
        "server_instance_id": app[server_instance_id_key],
    })

    async def _send(ws):
        try:
            await asyncio.wait_for(ws.send_str(payload), timeout=5.0)
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            return ws
        return None

    clients = list(app[ws_clients_key])
    if not clients:
        return
    results = await asyncio.gather(*[_send(ws) for ws in clients])
    for ws in results:
        if ws is not None:
            app[ws_clients_key].discard(ws)
