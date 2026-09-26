"""Tests for the aiohttp web server and WebSocket handling."""

import asyncio
import json
import pathlib
import time

import pytest
import pytest_asyncio
from aiohttp import test_utils, web

from server.race_state import RaceState
from server.server import (
    PAGE_VERSION_TOKEN,
    _read_release_version,
    broadcast,
    create_app,
    feed_state_key,
    page_version_key,
    ws_clients_key,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


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
async def test_index_sets_revalidation_headers(client):
    """The page must be revalidated, not reused from cache indefinitely.

    Before this, ``handle_index`` set no validator at all, so a browser was free to
    keep a copy for as long as it liked -- which is how a phone came to run a page
    older than the server it was talking to.
    """
    resp = await client.get("/")
    assert resp.headers["Cache-Control"] == "no-cache"
    etag = resp.headers["ETag"]
    assert etag.startswith('"') and etag.endswith('"')
    assert etag.strip('"')


@pytest.mark.asyncio
async def test_index_returns_304_to_a_matching_if_none_match(client):
    """A validator with no 304 path would re-send the whole page every revalidation."""
    first = await client.get("/")
    etag = first.headers["ETag"]

    resp = await client.get("/", headers={"If-None-Match": etag})
    assert resp.status == 304
    assert await resp.text() == ""


@pytest.mark.asyncio
async def test_index_returns_200_to_a_stale_if_none_match(client):
    """The 304 path must not swallow a genuinely changed page -- the bug being fixed."""
    resp = await client.get("/", headers={"If-None-Match": '"0000000000"'})
    assert resp.status == 200
    assert "SMART Live Timing" in await resp.text()


@pytest.mark.asyncio
async def test_the_served_page_carries_its_version_and_no_placeholder(client, app):
    """An unsubstituted token would leave every page claiming the same version.

    The page half of the handshake compares a literal baked into the copy it was
    served as; if the substitution silently stopped happening, every page would agree
    with every server forever and the prompt would never appear.
    """
    text = await (await client.get("/")).text()
    assert app[page_version_key] in text
    assert PAGE_VERSION_TOKEN not in text


@pytest.mark.asyncio
async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["version"] == (ROOT / "VERSION").read_text().strip()


def test_release_version_falls_back_when_the_version_file_is_missing(tmp_path):
    assert _read_release_version(version_file=tmp_path / "VERSION") == "0.0.0-dev"


def test_release_version_falls_back_when_the_version_file_is_empty(tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("\n")
    assert _read_release_version(version_file=version_file) == "0.0.0-dev"


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
async def test_ws_full_message_carries_the_page_version(app, client):
    """A client that connects and never sees an update still learns the version."""
    async with client.ws_connect("/ws") as ws:
        msg = await ws.receive_json()
        assert msg["page_version"] == app[page_version_key]


@pytest.mark.asyncio
async def test_broadcast_carries_the_page_version(app, client):
    """``full`` on connect is not enough.

    The phone in the reported incident was connected *through* the deploy, so the only
    message that could tell it the page had changed was a broadcast.
    """
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json()  # the initial "full"
        await broadcast(app, "update", {"test": True})
        msg = await ws.receive_json()
        assert msg["page_version"] == app[page_version_key]


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


_CLASS_CODES_MSG = {
    "type": "class_codes",
    "run_id": "0x4000AAAA",
    "entries": [{
        "entrant_id": "e1", "kind": "added", "number": "7",
        "class_name": "Saloon Cup", "transponder": "1234567", "class_code": "SC",
    }],
}


@pytest.mark.asyncio
async def test_ingested_class_codes_reach_the_snapshot(client, app):
    from server.server import race_state_key

    headers = {"Authorization": "Bearer test-secret"}
    for msg in (
        {"type": "class_info", "unique_number": "1", "description": "Saloon Cup"},
        {"type": "competitor", "reg_number": "7", "number": "7", "transponder": "1234567",
         "first_name": "Ann", "last_name": "Example", "nationality": "", "class_number": "1"},
        _CLASS_CODES_MSG,
    ):
        resp = await client.post("/api/ingest", json=msg, headers=headers)
        assert resp.status == 200
    snap = app[race_state_key].snapshot()
    car = next(e for e in snap["entries"] if e["reg_number"] == "7")
    assert car["class_code"] == "SC"
    assert snap["class_codes_available"] is True


@pytest.mark.asyncio
async def test_class_codes_arriving_while_the_feed_is_lost_are_not_a_recovery(client, app):
    """The class-code source is not the timing feed: its POSTs keep coming while
    :50000 is down, and must neither clear the outage nor reset the race state."""
    from server.server import race_state_key

    fs = app[feed_state_key]
    fs["feed_lost"] = True
    fs["last_ingest_at"] = stale = time.monotonic() - 3600
    async with client.ws_connect("/ws") as ws:
        assert (await ws.receive_json())["event"] == "full"
        assert (await ws.receive_json())["event"] == "no_feed"
        resp = await client.post(
            "/api/ingest", json=_CLASS_CODES_MSG,
            headers={"Authorization": "Bearer test-secret"},
        )
        assert resp.status == 200
        with pytest.raises(asyncio.TimeoutError):
            await ws.receive_json(timeout=0.2)
    assert fs["feed_lost"] is True
    assert fs["last_ingest_at"] == stale
    state = app[race_state_key]
    assert "1" in state.competitors  # not reset
    assert state.class_codes


def _preload_msg(n=1):
    """A registry preload of *n* synthetic entries; the first is car 7's."""
    return {
        "type": "class_code_preload",
        "age_seconds": 1.5,
        "entries": [
            {"transponder": str(1234567 + i), "class_name": "Saloon Cup", "class_code": "SC"}
            for i in range(n)
        ],
    }


async def _post_car_7_and(client, msg):
    headers = {"Authorization": "Bearer test-secret"}
    for m in (
        {"type": "class_info", "unique_number": "1", "description": "Saloon Cup"},
        {"type": "competitor", "reg_number": "7", "number": "7", "transponder": "1234567",
         "first_name": "Ann", "last_name": "Example", "nationality": "", "class_number": "1"},
        msg,
    ):
        resp = await client.post("/api/ingest", json=m, headers=headers)
        assert resp.status == 200


@pytest.mark.asyncio
async def test_an_ingested_preload_reaches_the_snapshot(client, app):
    from server.server import race_state_key

    await _post_car_7_and(client, _preload_msg())
    snap = app[race_state_key].snapshot()
    car = next(e for e in snap["entries"] if e["reg_number"] == "7")
    assert car["class_code"] == "SC"
    assert snap["class_codes_available"] is True


@pytest.mark.asyncio
async def test_a_realistic_registry_preload_fits_the_ingest_limit(client, app):
    """A preload is ~400 KB today and grows; aiohttp's default 1 MiB would
    reject a larger archive with a 413 on every retry."""
    from server.server import race_state_key

    # About three times today's archive, and past the default limit.
    msg = _preload_msg(15_000)
    assert len(json.dumps(msg)) > 1024 ** 2
    await _post_car_7_and(client, msg)
    snap = app[race_state_key].snapshot()
    assert next(e for e in snap["entries"] if e["reg_number"] == "7")["class_code"] == "SC"


def test_the_ingest_limit_matches_the_relays_preload_ceiling(app):
    """The relay withholds a preload over its ceiling rather than retry a 413."""
    from relay.class_code_client import MAX_PRELOAD_BYTES

    assert app._client_max_size == MAX_PRELOAD_BYTES


@pytest.mark.asyncio
async def test_a_preload_arriving_while_the_feed_is_lost_is_not_a_recovery(client, app):
    from server.server import race_state_key

    fs = app[feed_state_key]
    fs["feed_lost"] = True
    fs["last_ingest_at"] = stale = time.monotonic() - 3600
    resp = await client.post(
        "/api/ingest", json=_preload_msg(),
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status == 200
    assert fs["feed_lost"] is True
    assert fs["last_ingest_at"] == stale
    state = app[race_state_key]
    assert "1" in state.competitors  # not reset
    assert state.class_code_preload["entries"]


@pytest.mark.asyncio
async def test_class_codes_during_an_outage_do_not_broadcast_an_update(client, app):
    """The production broadcast path sends no ``update`` while the feed is lost.

    The page hides its no-feed notice on any ``update``, and the watchdog does
    not repeat ``no_feed``, so a class-code batch broadcast mid-outage would
    hide the outage for good.  Recovery then carries the codes in its ``full``.
    """
    from server.server import broadcast_if_dirty, race_state_key

    headers = {"Authorization": "Bearer test-secret"}
    state = app[race_state_key]
    state.mark_clean()
    app[feed_state_key]["feed_lost"] = True
    async with client.ws_connect("/ws") as ws:
        assert (await ws.receive_json())["event"] == "full"
        assert (await ws.receive_json())["event"] == "no_feed"
        resp = await client.post("/api/ingest", json=_CLASS_CODES_MSG, headers=headers)
        assert resp.status == 200
        assert state.dirty
        assert await broadcast_if_dirty(app) is False
        with pytest.raises(asyncio.TimeoutError):
            await ws.receive_json(timeout=0.2)
        assert state.dirty  # kept for the recovery broadcast

        resp = await client.post(
            "/api/ingest",
            json={"type": "class_info", "unique_number": "1", "description": "Saloon Cup"},
            headers=headers,
        )
        assert resp.status == 200
        assert (await ws.receive_json(timeout=1.0))["event"] == "full"
    assert state.class_codes


@pytest.mark.asyncio
async def test_broadcast_if_dirty_sends_one_update_when_the_feed_is_live(client, app):
    from server.server import broadcast_if_dirty, race_state_key

    state = app[race_state_key]
    async with client.ws_connect("/ws") as ws:
        assert (await ws.receive_json())["event"] == "full"
        state.process(_CLASS_CODES_MSG)
        assert await broadcast_if_dirty(app) is True
        assert (await ws.receive_json(timeout=1.0))["event"] == "update"
        assert not state.dirty
        assert await broadcast_if_dirty(app) is False


@pytest.mark.asyncio
async def test_feed_restored_reset_keeps_class_codes(client, app):
    from server.server import race_state_key

    headers = {"Authorization": "Bearer test-secret"}
    resp = await client.post("/api/ingest", json=_CLASS_CODES_MSG, headers=headers)
    assert resp.status == 200
    app[feed_state_key]["feed_lost"] = True
    # The first message after the outage triggers the reset; the feed then
    # repopulates the class and the car.
    for msg in (
        {"type": "class_info", "unique_number": "1", "description": "Saloon Cup"},
        {"type": "competitor", "reg_number": "7", "number": "7", "transponder": "1234567",
         "first_name": "Ann", "last_name": "Example", "nationality": "", "class_number": "1"},
    ):
        resp = await client.post("/api/ingest", json=msg, headers=headers)
        assert resp.status == 200
    assert app[feed_state_key]["feed_lost"] is False
    # The reset dropped the fixture's car 1, so car 7 is the only entry.
    (car,) = app[race_state_key].snapshot()["entries"]
    assert car["class_code"] == "SC"


@pytest.mark.asyncio
async def test_repeated_init_wipes_reach_clients_before_repopulation(client):
    """Three ``$I`` records in a row each wipe live state and tell the clients.

    A live session start sent ``$I`` three times inside two milliseconds, then a
    duplicated ``$B`` and the competitor dump.  This posts that sequence through
    the real ``/api/ingest`` path with a WebSocket client attached, so it pins
    what the unit tests cannot: every init is broadcast as it lands, each one
    carrying an *empty* snapshot, and the session is only whole again because
    the repopulating records follow in the same batch.  A change that deferred,
    coalesced or reordered those broadcasts would fail here.
    """
    async with client.ws_connect("/ws") as ws:
        assert (await ws.receive_json())["event"] == "full"

        headers = {"Authorization": "Bearer test-secret"}
        for _ in range(3):
            resp = await client.post(
                "/api/ingest",
                json={"type": "init", "time_of_day": "15:29:14", "date": "12 Sep 26"},
                headers=headers,
            )
            assert resp.status == 200
            msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            assert msg["event"] == "init"
            # The wipe is visible to clients at the moment it happens: the
            # fixture's competitor is gone from the very first one.
            assert msg["data"]["entries"] == []

        for _ in range(2):
            await client.post(
                "/api/ingest",
                json={"type": "run", "unique_number": "33",
                      "description": "Race 6 - Final 12a"},
                headers=headers,
            )
        await client.post(
            "/api/ingest",
            json={"type": "competitor", "reg_number": "79", "number": "79",
                  "first_name": "Paul", "last_name": "Brydon",
                  "nationality": "Solution F BMW M3", "class_number": "1"},
            headers=headers,
        )

    resp = await client.get("/api/state")
    data = await resp.json()
    assert data["run_description"] == "Race 6 - Final 12a"
    assert [e["reg_number"] for e in data["entries"]] == ["79"]


@pytest.mark.asyncio
async def test_ingest_with_empty_relay_secret_allows_no_auth_header(race_state):
    """RELAY_SECRET='' is a documented way to disable ingest auth entirely."""
    app = create_app(race_state, relay_secret="")
    async with test_utils.TestClient(test_utils.TestServer(app)) as client:
        resp = await client.post(
            "/api/ingest",
            json={"type": "heartbeat", "laps_to_go": "5", "time_to_go": "00:05:00",
                  "time_of_day": "14:00:00", "race_time": "00:05:00", "flag": "Green"},
        )
        assert resp.status == 200


@pytest.mark.asyncio
async def test_ingest_with_empty_relay_secret_allows_any_auth_header(race_state):
    """Confirms auth is truly bypassed when RELAY_SECRET is empty, not just
    lenient about a missing header."""
    app = create_app(race_state, relay_secret="")
    async with test_utils.TestClient(test_utils.TestServer(app)) as client:
        resp = await client.post(
            "/api/ingest",
            json={"type": "heartbeat", "laps_to_go": "5", "time_to_go": "00:05:00",
                  "time_of_day": "14:00:00", "race_time": "00:05:00", "flag": "Green"},
            headers={"Authorization": "garbage-value"},
        )
        assert resp.status == 200


@pytest.mark.asyncio
async def test_ingest_missing_type_returns_400(client):
    resp = await client.post(
        "/api/ingest",
        json={"flag": "Green"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_type", [["class_codes"], {"a": 1}, 7, None])
async def test_ingest_non_string_type_returns_400(client, bad_type):
    resp = await client.post(
        "/api/ingest",
        json={"type": bad_type},
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
async def test_no_feed_sent_immediately_on_connect_when_feed_already_lost(app, client):
    """A client connecting while the feed is lost receives no_feed right after full."""
    app[feed_state_key]["feed_lost"] = True
    async with client.ws_connect("/ws") as ws:
        first = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert first["event"] == "full"
        second = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert second["event"] == "no_feed"


@pytest.mark.asyncio
async def test_no_feed_sent_immediately_when_state_restored_at_startup(race_state):
    """A server started with restored (persisted) state seeds feed_lost=True so a
    connecting client is told not to trust the snapshot until fresh data arrives."""
    restored_app = create_app(race_state, relay_secret="test-secret", restored=True)
    async with test_utils.TestClient(test_utils.TestServer(restored_app)) as client:
        async with client.ws_connect("/ws") as ws:
            first = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            assert first["event"] == "full"
            second = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            assert second["event"] == "no_feed"


@pytest.mark.asyncio
async def test_no_feed_sent_immediately_on_connect_when_timed_out(app, client):
    """A client connecting when last_ingest_at is stale receives no_feed right away."""
    app[feed_state_key]["last_ingest_at"] = time.monotonic() - 400  # beyond 300 s threshold
    app[feed_state_key]["feed_lost"] = False
    async with client.ws_connect("/ws") as ws:
        first = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert first["event"] == "full"
        second = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
        assert second["event"] == "no_feed"
        # Flag should now be set so watchdog won't double-fire
        assert app[feed_state_key]["feed_lost"] is True

@pytest.mark.asyncio
async def test_no_feed_watchdog_broadcasts_no_feed(app, client, monkeypatch):
    """Watchdog sends 'no_feed' event after the timeout threshold is exceeded.

    Drives the real ``_feed_watchdog`` coroutine (rather than reimplementing
    its condition inline) by patching its 30-second poll sleep so the first
    iteration fires immediately and the second raises CancelledError, which
    the watchdog's own try/except treats as a normal stop.
    """
    async with client.ws_connect("/ws") as ws:
        await ws.receive_json()  # consume initial 'full'

        # Simulate a feed that has been silent for longer than the threshold
        fs = app[feed_state_key]
        fs["last_ingest_at"] = time.monotonic() - 400  # 400 s ago > 300 s threshold
        fs["feed_lost"] = False

        from server.server import _feed_watchdog

        real_sleep = asyncio.sleep
        sleep_calls = 0

        async def fast_sleep(_delay):
            # NB: this patches the shared `asyncio` module, so it must call
            # the captured real sleep rather than asyncio.sleep to avoid
            # recursing into itself.
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError
            await real_sleep(0)

        monkeypatch.setattr("server.server.asyncio.sleep", fast_sleep)
        await _feed_watchdog(app)

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
async def test_feed_recovery_clears_stale_entrants(app, client):
    """Entrants from before a feed outage must not merge with the new race.

    Regression test: if the feed drops out (e.g. DNS failures on the relay)
    for long enough to trip the no-feed watchdog, and a session-change ($I)
    message is missed during the outage, competitors from the old race must
    not linger and get mixed in with the new race's entrants once the feed
    reconnects.
    """
    from server.server import race_state_key

    assert len(app[race_state_key].competitors) == 1  # "Alice" from the fixture

    app[feed_state_key]["feed_lost"] = True

    resp = await client.post(
        "/api/ingest",
        json={
            "type": "competitor",
            "reg_number": "2",
            "number": "2",
            "first_name": "Bob",
            "last_name": "Racer",
            "nationality": "GBR",
            "class_number": "1",
        },
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status == 200

    competitors = app[race_state_key].competitors
    assert "1" not in competitors  # stale entrant from before the outage is gone
    assert "2" in competitors  # only the new race's entrant remains


@pytest.mark.asyncio
async def test_ingest_records_last_ingest_at(app, client):
    """Successful ingest updates last_ingest_at (which is pre-seeded at startup)."""
    # last_ingest_at is seeded at startup time, not None
    assert app[feed_state_key]["last_ingest_at"] is not None
    before = time.monotonic()
    resp = await client.post(
        "/api/ingest",
        json={"type": "heartbeat", "laps_to_go": "5", "time_to_go": "00:05:00",
              "time_of_day": "14:00:00", "race_time": "00:05:00", "flag": "Green"},
        headers={"Authorization": "Bearer test-secret"},
    )
    assert resp.status == 200
    assert app[feed_state_key]["last_ingest_at"] >= before


@pytest.mark.asyncio
async def test_api_state_includes_gap_and_diff_fields(client):
    """The derived interval fields reach the browser.

    ``snapshot`` emits competitor dicts whole with no per-field allowlist, so
    this asserts the payload contract the page actually receives.  The fixture
    has a single competitor, which is P1 and therefore has no interval, so this
    asserts presence rather than truthiness.
    """
    resp = await client.get("/api/state")
    assert resp.status == 200
    data = await resp.json()
    entry = data["entries"][0]
    for field in (
        "gap_ahead_seconds", "gap_ahead_laps",
        "diff_leader_seconds", "diff_leader_laps",
    ):
        assert field in entry
        assert entry[field] is None
