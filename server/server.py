"""aiohttp web server with WebSocket push for live race state."""

import asyncio
import hmac
import json
import logging
import os
import pathlib
import time
import uuid

from aiohttp import web

log = logging.getLogger(__name__)
TEMPLATES = pathlib.Path(__file__).parent / "templates"

BROADCAST_INTERVAL = float(os.environ.get("BROADCAST_INTERVAL", "0.25"))
NO_FEED_TIMEOUT = float(os.environ.get("NO_FEED_TIMEOUT", "300"))  # seconds

# Typed app keys (avoids NotAppKeyWarning)
race_state_key = web.AppKey("race_state")
ws_clients_key = web.AppKey("ws_clients", set)
server_instance_id_key = web.AppKey("server_instance_id", str)
relay_secret_key = web.AppKey("relay_secret", str)
# Mutable feed-state dict; mutate contents rather than reassigning the key.
# Keys: "last_ingest_at" (float|None), "feed_lost" (bool), "watchdog_task" (Task|None)
feed_state_key = web.AppKey("feed_state", dict)


def _feed_state(app) -> dict:
    return app[feed_state_key]


def create_app(race_state, relay_secret: str = "", restored: bool = False) -> web.Application:
    app = web.Application()
    app[race_state_key] = race_state
    app[ws_clients_key] = set()
    app[server_instance_id_key] = str(uuid.uuid4())
    app[relay_secret_key] = relay_secret
    app[feed_state_key] = {"last_ingest_at": None, "feed_lost": restored, "watchdog_task": None}

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_post("/api/ingest", handle_ingest)

    app.on_startup.append(_start_watchdog)
    app.on_cleanup.append(_stop_watchdog)

    return app


async def handle_index(request: web.Request) -> web.Response:
    html = (TEMPLATES / "index.html").read_text()
    return web.Response(text=html, content_type="text/html")


async def handle_api_state(request: web.Request) -> web.Response:
    state = request.app[race_state_key]
    return web.json_response(state.snapshot())


async def handle_healthz(request: web.Request) -> web.Response:
    for key in ("broadcast_task", "save_task"):
        task = request.app.get(key)
        if task is not None and task.done():
            return web.json_response(
                {"status": "error", "detail": f"{key} has stopped"}, status=503
            )
    watchdog = _feed_state(request.app).get("watchdog_task")
    if watchdog is not None and watchdog.done():
        return web.json_response(
            {"status": "error", "detail": "watchdog_task has stopped"}, status=503
        )
    return web.json_response({"status": "ok"})


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    clients: set = request.app[ws_clients_key]
    clients.add(ws)
    state = request.app[race_state_key]
    instance_id = request.app[server_instance_id_key]
    log.info("WebSocket client connected (%d total)", len(clients))
    try:
        # Send the full current state on connect, including the server instance ID
        # so clients can detect a server restart and reload the page.
        await ws.send_json({
            "event": "full",
            "data": state.snapshot(),
            "server_instance_id": instance_id,
        })
        # If the feed is already known to be lost (or timed out before the
        # watchdog's next poll), tell this client immediately so it doesn't
        # sit showing hours-old stale data until the next watchdog tick.
        fs = _feed_state(request.app)
        feed_timed_out = (time.monotonic() - fs["last_ingest_at"]) > NO_FEED_TIMEOUT
        if fs["feed_lost"] or feed_timed_out:
            if not fs["feed_lost"]:
                fs["feed_lost"] = True  # sync flag so watchdog won't double-fire
            await ws.send_json({"event": "no_feed", "data": {}, "server_instance_id": instance_id})
        async for _msg in ws:
            pass  # We don't expect client-to-server messages
    finally:
        clients.discard(ws)
        log.info("WebSocket client disconnected (%d remaining)", len(clients))
    return ws


async def handle_ingest(request: web.Request) -> web.Response:
    """Receive a parsed rMonitor message from the relay and apply it to state."""
    secret = request.app[relay_secret_key]
    auth = request.headers.get("Authorization", "")
    if secret and not hmac.compare_digest(auth, f"Bearer {secret}"):
        raise web.HTTPUnauthorized(reason="Invalid relay secret")
    try:
        msg = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Invalid JSON body")
    if not isinstance(msg, dict) or "type" not in msg:
        raise web.HTTPBadRequest(reason="Missing 'type' field")

    fs = _feed_state(request.app)
    fs["last_ingest_at"] = time.monotonic()
    was_lost = fs["feed_lost"]
    state = request.app[race_state_key]
    if was_lost:
        fs["feed_lost"] = False
        log.warning(
            "Timing feed restored after an extended outage – resetting race "
            "state, since a session change ($I) may have been missed while "
            "the feed was down"
        )
        state.reset()

    try:
        event = state.process(msg)
    except Exception:
        log.exception("Error processing ingest message: %s", msg)
        raise web.HTTPBadRequest(reason="Message could not be processed")
    if event == "init":
        await broadcast(request.app, "init", state.snapshot())
        state.mark_clean()
    elif was_lost:
        # Feed just came back — push full state so clients dismiss the modal.
        await broadcast(request.app, "full", state.snapshot())
        state.mark_clean()
    return web.json_response({"status": "ok"})


async def _feed_watchdog(app: web.Application) -> None:
    """Background task: broadcast 'no_feed' if ingest has been silent for too long."""
    fs = _feed_state(app)
    try:
        while True:
            await asyncio.sleep(30)
            if fs["feed_lost"]:
                continue
            last = fs["last_ingest_at"]
            if last is not None and (time.monotonic() - last) > NO_FEED_TIMEOUT:
                log.warning("No timing feed received for %.0f seconds — notifying clients", NO_FEED_TIMEOUT)
                fs["feed_lost"] = True
                await broadcast(app, "no_feed", {})
    except asyncio.CancelledError:
        pass


async def _start_watchdog(app: web.Application) -> None:
    # Seed last_ingest_at to now so the 5-minute clock starts at server
    # startup. Without this, a server that never receives any relay data
    # would never trigger the no_feed modal (because last_ingest_at stays None).
    _feed_state(app)["last_ingest_at"] = time.monotonic()
    _feed_state(app)["watchdog_task"] = asyncio.create_task(_feed_watchdog(app))


async def _stop_watchdog(app: web.Application) -> None:
    task = _feed_state(app).get("watchdog_task")
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
