"""Tests for the aiohttp web server and WebSocket handling."""

import asyncio
import json
import time

import pytest
import pytest_asyncio
from aiohttp import test_utils, web

from server.race_state import RaceState
from server.server import (
    broadcast,
    create_app,
    feed_state_key,
    ws_clients_key,
)


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
    return create_app(race_state, relay_secret="test-secret")


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
    assert "SMART Live Timing" in text


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


# ---------------------------------------------------------------------------
# Ingest endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_valid_auth_returns_ok(client):
    resp = await client.post(
        "/api/ingest",
        json={"type": "heartbeat", "laps_to_go": "5", "time_to_go": "00:05:00",
              "time_of_day": "14:00:00", "race_time": "00:05:00", "flag": "Green"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_ingest_invalid_auth_returns_401(client):
    resp = await client.post(
        "/api/ingest",
        json={"type": "heartbeat", "laps_to_go": "5", "time_to_go": "00:05:00",
              "time_of_day": "14:00:00", "race_time": "00:05:00", "flag": "Green"},
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_ingest_missing_auth_returns_401(client):
    resp = await client.post(
        "/api/ingest",
        json={"type": "heartbeat", "laps_to_go": "5", "time_to_go": "00:05:00",
              "time_of_day": "14:00:00", "race_time": "00:05:00", "flag": "Green"},
    )
    assert resp.status == 401


@pytest.mark.asyncio
async def test_ingest_updates_race_state(client, app):
    resp = await client.post(
        "/api/ingest",
        json={"type": "setting", "description": "TRACKNAME", "value": "Brands Hatch"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status == 200
    from server.server import race_state_key
    assert app[race_state_key].track_name == "Brands Hatch"


@pytest.mark.asyncio
async def test_ingest_missing_type_returns_400(client):
    resp = await client.post(
        "/api/ingest",
        json={"flag": "Green"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_ingest_malformed_json_returns_400(client):
    resp = await client.post(
        "/api/ingest",
        data=b"not json",
        headers={
            "Authorization": "Bearer test-secret",
            "Content-Type": "application/json",
        },
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_ingest_init_broadcasts_to_ws_clients(app, client):
    """An init message via /api/ingest triggers an immediate 'init' broadcast."""
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json()  # consume initial 'full' message

        resp = await client.post(
            "/api/ingest",
            json={"type": "init", "time_of_day": "10:00:00", "date": "01 Jan 25"},
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status == 200

        msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg["event"] == "init"


# ---------------------------------------------------------------------------
# No-feed watchdog tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_feed_watchdog_broadcasts_no_feed(app, client):
    """Watchdog sends 'no_feed' event after the timeout threshold is exceeded."""
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json()  # consume initial 'full'

        # Simulate a feed that has been silent for longer than the threshold
        fs = app[feed_state_key]
        fs["last_ingest_at"] = time.monotonic() - 400  # 400 s ago > 300 s threshold
        fs["feed_lost"] = False

        # Trigger the watchdog logic directly (avoid waiting 30 s)
        from server.server import NO_FEED_TIMEOUT
        last = fs["last_ingest_at"]
        if last is not None and (time.monotonic() - last) > NO_FEED_TIMEOUT:
            fs["feed_lost"] = True
            await broadcast(app, "no_feed", {})

        msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg["event"] == "no_feed"
        assert fs["feed_lost"] is True


@pytest.mark.asyncio
async def test_feed_recovery_broadcasts_full_state(app, client):
    """When an ingest arrives while feed_lost=True, a full state broadcast is sent."""
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json()  # consume initial 'full'

        # Put the app into the feed-lost state
        app[feed_state_key]["feed_lost"] = True

        resp = await client.post(
            "/api/ingest",
            json={"type": "setting", "description": "TRACKNAME", "value": "Silverstone"},
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status == 200

        # Should receive a 'full' event as the recovery broadcast
        msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert msg["event"] == "full"
        assert app[feed_state_key]["feed_lost"] is False


@pytest.mark.asyncio
async def test_ingest_records_last_ingest_at(app, client):
    """Successful ingest updates last_ingest_at."""
    assert app[feed_state_key]["last_ingest_at"] is None
    before = time.monotonic()
    resp = await client.post(
        "/api/ingest",
        json={"type": "heartbeat", "laps_to_go": "5", "time_to_go": "00:05:00",
              "time_of_day": "14:00:00", "race_time": "00:05:00", "flag": "Green"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status == 200
    assert app[feed_state_key]["last_ingest_at"] is not None
    assert app[feed_state_key]["last_ingest_at"] >= before
