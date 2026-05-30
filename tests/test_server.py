"""Tests for the aiohttp web server and WebSocket handling."""

import asyncio
import json

import pytest
import pytest_asyncio
from aiohttp import test_utils, web

from app.race_state import RaceState
from app.server import broadcast, create_app, ws_clients_key


@pytest.fixture
def race_state():
    rs = RaceState()
    rs.process({"type": "setting", "description": "TRACKNAME", "value": "Test Track"})
    rs.process({
        "type": "competitor",
        "reg_number": "1",
        "number": "1",
        "first_name": "Alice",
        "last_name": "Driver",
        "nationality": "GBR",
        "class_number": "1",
    })
    rs.process({
        "type": "race_info",
        "position": "1",
        "reg_number": "1",
        "laps": "3",
        "total_time": "00:05:00.000",
    })
    return rs


@pytest.fixture
def app(race_state):
    return create_app(race_state)


@pytest_asyncio.fixture
async def client(app):
    async with test_utils.TestClient(test_utils.TestServer(app)) as c:
        yield c


@pytest.mark.asyncio
async def test_index_returns_html(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert "text/html" in resp.content_type
    text = await resp.text()
    assert "rMonitor Live Timing" in text


@pytest.mark.asyncio
async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_api_state(client):
    resp = await client.get("/api/state")
    assert resp.status == 200
    data = await resp.json()
    assert data["track_name"] == "Test Track"
    assert len(data["entries"]) == 1
    assert data["entries"][0]["first_name"] == "Alice"


@pytest.mark.asyncio
async def test_websocket_sends_full_state_on_connect(client):
    async with client.ws_connect("/ws") as ws:
        msg = await ws.receive_json()
        assert msg["event"] == "full"
        assert msg["data"]["track_name"] == "Test Track"
        assert "server_instance_id" in msg


@pytest.mark.asyncio
async def test_broadcast_delivers_to_clients(app, client):
    async with client.ws_connect("/ws") as ws:
        # Consume the initial "full" message
        await ws.receive_json()

        # Broadcast an update
        await broadcast(app, "update", {"test": True})
        msg = await ws.receive_json()
        assert msg["event"] == "update"
        assert msg["data"]["test"] is True


@pytest.mark.asyncio
async def test_broadcast_removes_closed_clients(app, client):
    ws = await client.ws_connect("/ws")
    await ws.receive_json()  # initial full state
    assert len(app[ws_clients_key]) == 1

    await ws.close()
    # Give the server a moment to process the close
    await asyncio.sleep(0.05)

    # Broadcast should clean up the closed client
    await broadcast(app, "update", {"test": True})
    assert len(app[ws_clients_key]) == 0
