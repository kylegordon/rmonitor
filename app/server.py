"""aiohttp web server with WebSocket push for live race state."""

import asyncio
import json
import logging
import pathlib

from aiohttp import web

log = logging.getLogger(__name__)
TEMPLATES = pathlib.Path(__file__).parent / "templates"


def create_app(race_state) -> web.Application:
    app = web.Application()
    app["race_state"] = race_state
    app["ws_clients"] = set()

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/state", handle_api_state)

    return app


async def handle_index(request: web.Request) -> web.Response:
    html = (TEMPLATES / "index.html").read_text()
    return web.Response(text=html, content_type="text/html")


async def handle_api_state(request: web.Request) -> web.Response:
    state = request.app["race_state"]
    return web.json_response(state.snapshot())


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients: set = request.app["ws_clients"]
    clients.add(ws)
    state = request.app["race_state"]
    log.info("WebSocket client connected (%d total)", len(clients))
    try:
        # Send the full current state on connect
        await ws.send_json({"event": "full", "data": state.snapshot()})
        async for _msg in ws:
            pass  # We don't expect client-to-server messages
    finally:
        clients.discard(ws)
        log.info("WebSocket client disconnected (%d remaining)", len(clients))
    return ws


async def broadcast(app: web.Application, event: str, data: dict):
    """Send a JSON message to every connected WebSocket client."""
    payload = json.dumps({"event": event, "data": data})
    closed = []
    for ws in app["ws_clients"]:
        try:
            await ws.send_str(payload)
        except (ConnectionError, RuntimeError):
            closed.append(ws)
    for ws in closed:
        app["ws_clients"].discard(ws)
