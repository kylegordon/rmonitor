# Copilot Instructions for rmonitor

## Git Workflow — MANDATORY

**All changes must go through a pull request. Never commit directly to `master`.**

1. **Fetch first**: `git fetch origin` — always do this before branching or pushing
2. **Create a branch**: `git checkout -b copilot/<short-description> origin/master`
3. Make commits on the branch
4. **Check PR status** before pushing: `gh pr list --head <branch>` — if a PR for this branch already exists and is merged/closed, create a new branch instead
5. **Push**: `git push -u origin copilot/<short-description>`
6. **Open a PR**: `gh pr create --base master --fill`
7. Do **not** merge or push to `master` directly under any circumstances

This applies to every change, no matter how small.

## Project Overview

`rmonitor` is a Python asyncio application split into two components — a lightweight **relay** that runs on-premise near the timing hardware, and a **server** that runs in the cloud and serves the live leaderboard. Both are deployed as Docker containers.

## Technology Stack

- **Python 3.12** — all async/await patterns using `asyncio`
- **aiohttp ≥ 3.9, < 4** — the only runtime dependency (HTTP client in relay; web server in server)
- **pytest + pytest-asyncio** — for testing (`pip install -r requirements-dev.txt`), plus each component's own `requirements*.txt` (see Pitfall 11)
- **Docker / Docker Compose** — for containerised deployment
- No database, no ORM, no frontend build step

## Repository Layout

```
relay/
├── rmonitor_client.py  # Async TCP client + full rMonitor protocol parser
├── main.py             # Entry point: connects to feed, POSTs messages to server
├── __main__.py         # `python -m relay` entry point
├── requirements.txt    # aiohttp only
└── Dockerfile          # Build context: repo root

server/
├── race_state.py       # In-memory race state, updated from parsed messages
├── state_store.py      # StateStore ABC + JsonFileStateStore (pluggable for Lambda)
├── server.py           # aiohttp routes: HTML page, /ws WebSocket, /api/state, /api/ingest
├── main.py             # Entry point: starts web server (no TCP client)
├── __main__.py         # `python -m server` entry point
├── requirements.txt    # aiohttp only
├── Dockerfile          # Build context: repo root
└── templates/
    └── index.html      # Single-page HTML leaderboard (vanilla JS + WebSocket)

tests/
├── test_parser.py      # Unit tests for the protocol parser (relay.rmonitor_client)
├── test_race_state.py  # Unit tests for RaceState (server.race_state)
├── test_server.py      # aiohttp TestClient tests incl. /api/ingest
└── test_integration.py # Replay sample capture files end-to-end

examples/               # Sample AMB rMonitor capture files (real Sebring data)
captures/               # (runtime) destination for rmonitor_capture.py output
rmonitor_send.py        # Dev helper: TCP server that replays a sample file
rmonitor_capture.py     # Diagnostic tool: captures a live feed to a timestamped log
docker-compose.yml      # Both services; optional `tunnel` profile adds cloudflared
```

## Architecture

```
[rMonitor timing system]
        ↓ TCP (port 50000)
┌──────────────────────┐
│  relay               │  POST /api/ingest   ┌──────────────────────────┐
│  RMonitorClient.run()│ ─────────────────→  │  server                  │
│  (auto-reconnects)   │  Bearer <secret>    │  aiohttp web server      │
└──────────────────────┘                     │  ├── GET /               │
                                             │  ├── GET /ws (WebSocket) │
                                             │  ├── GET /api/state      │
                                             │  └── POST /api/ingest    │
                                             └──────────────────────────┘
                                                        ↓ WebSocket
                                                 [Browser clients]
```

- **Relay** parses each TCP line → dict → POSTs to `/api/ingest` with `Authorization: Bearer <RELAY_SECRET>`. Retries on 5xx/429 with capped exponential backoff, giving up after `RETRY_MAX_ATTEMPTS`; drops on other 4xx; exits immediately (for the container to restart) on connection-level/network errors.
- **Server** applies messages to `RaceState` and broadcasts to WebSocket clients. Returns 401 on bad key, 400 on bad/missing payload.
- **`StateStore`** (`server/state_store.py`) abstracts persistence: `JsonFileStateStore` for Docker, replaceable with DynamoDB/Redis for Lambda.
- **`dirty` flag** on `RaceState` prevents redundant WebSocket broadcasts.
- On a `$I` (init) message, state is cleared and an `"init"` event is broadcast immediately via the ingest handler (not the periodic loop).
- The aiohttp WebSocket uses a 30-second heartbeat to survive Cloudflare's 100-second idle timeout.
- A `server_instance_id` UUID lets clients detect a server restart and reload.

## Protocol Messages

The parser in `relay/rmonitor_client.py` handles these `$`-prefixed, comma-separated, double-quote-delimited lines:

| Token  | Parsed type    | Key fields |
|--------|----------------|------------|
| `$F`   | `heartbeat`    | flag, race_time, laps_to_go, time_to_go |
| `$A`   | `competitor`   | reg_number, number, first/last name, nationality, class_number |
| `$COMP`| `competitor`   | same as `$A` plus additional_data |
| `$B`   | `run`          | description (session/run name) |
| `$C`   | `class_info`   | unique_number, description |
| `$E`   | `setting`      | description (TRACKNAME / TRACKLENGTH), value |
| `$G`   | `race_info`    | position, reg_number, laps, total_time |
| `$H`   | `qual_info`    | position, reg_number, best_lap, best_lap_time |
| `$I`   | `init`         | Clears all state; new session begins |
| `$J`   | `passing`      | reg_number, lap_time, total_time |
| `$SP`  | `lap_info`     | position, reg_number, lap_number, lap_time (undocumented) |
| `$SR`  | `lap_info`     | same as `$SP` (undocumented) |

Adding a new message type: register a parser with `@_reg("$X")` in `relay/rmonitor_client.py`, add a handler on `RaceState` in `server/race_state.py`, and register it in `RaceState._HANDLERS`.

## Configuration (Environment Variables)

### Relay
| Variable        | Default         | Description                                  |
|-----------------|-----------------|----------------------------------------------|
| `RMONITOR_HOST` | `127.0.0.1`     | rMonitor feed hostname or IP                 |
| `RMONITOR_PORT` | `50000`         | rMonitor feed TCP port                       |
| `SERVER_URL`    | `http://localhost:8080` | Base URL of the server               |
| `RELAY_SECRET`  | *(empty)*       | Shared key; warning logged if unset          |
| `POST_TIMEOUT`  | `5.0`           | HTTP POST timeout (seconds)                  |
| `RETRY_DELAY`   | `1.0`           | Initial delay between retries on transient failure |
| `RETRY_MAX_DELAY` | `30.0`        | Cap on the exponential backoff retry delay   |
| `RETRY_MAX_ATTEMPTS` | `30`       | Give up and exit after this many transient-failure retries |

### Server
| Variable            | Default             | Description                              |
|---------------------|---------------------|------------------------------------------|
| `RELAY_SECRET`      | *(empty)*           | Must match relay's value; disables auth if empty |
| `WEB_HOST`          | `0.0.0.0`           | Web server bind address                  |
| `WEB_PORT`          | `8080`              | Web server port                          |
| `STATE_FILE`        | `data/state.json`   | Persistence path                         |
| `SAVE_INTERVAL`     | `10`                | How often to persist state (seconds)     |
| `BROADCAST_INTERVAL`| `0.25`              | WebSocket push interval (seconds)        |

## How to Run

### With Docker Compose (both components)
```bash
export RELAY_SECRET=change-me
export RMONITOR_HOST=192.168.10.24
docker compose up
# Browse to http://localhost:8080
```

Published images from GHCR are used by default. To build from local source
instead (e.g. to test unreleased changes), build the images first:

```bash
docker build -f relay/Dockerfile -t ghcr.io/kylegordon/rmonitor-relay:latest .
docker build -f server/Dockerfile -t ghcr.io/kylegordon/rmonitor-server:latest .
docker compose up
```

Alternatively, create a git-ignored `docker-compose.override.yml` that restores
`build:` contexts so `docker compose up --build` builds from source automatically
(see `docker-compose.override.yml` in the repo root for a ready-to-use template).

### Individually (development)
```bash
# Server
cd server && pip install -r requirements.txt
export RELAY_SECRET=dev && python -m server

# Relay
cd relay && pip install -r requirements.txt
export RELAY_SECRET=dev && export RMONITOR_HOST=127.0.0.1 && python -m relay
```

### Test with sample data (no real timing hardware)
```bash
# Terminal 1 – relay the Sebring sample on port 50000
python rmonitor_send.py

# Terminal 2 – server
export RELAY_SECRET=dev && python -m server

# Terminal 3 – relay pointing at the local test sender
export RELAY_SECRET=dev && python -m relay
```

### With Cloudflare Tunnel
```bash
export RELAY_SECRET=change-me
export RMONITOR_HOST=192.168.10.24
export CLOUDFLARE_TUNNEL_TOKEN=<token>
docker compose --profile tunnel up --build
```
Point the tunnel public hostname to `http://server:8080`.

## Testing

```bash
pip install -r requirements-dev.txt -r relay/requirements.txt -r relay/requirements-build.txt -r server/requirements.txt
python3 -m pytest tests/ -v

# Run a single test file
python3 -m pytest tests/test_server.py -v

# Run a single test by name
python3 -m pytest tests/test_server.py::test_ingest_valid_auth_returns_ok -v
```

- `test_parser.py` — raw protocol string → parsed dict
- `test_race_state.py` — message dicts → RaceState mutations and snapshot ordering
- `test_server.py` — aiohttp TestClient: WebSocket, `/api/state`, `/api/ingest` (auth, message processing, init broadcast)
- `test_integration.py` — replay full sample capture files through parser + RaceState

## Common Pitfalls and Workarounds

1. **`reg_number` vs `number`**: `reg_number` is the internal registration key (e.g. `"21"`). `number` is the displayed car number (may include letters, e.g. `"12X"`). Always key competitor dicts by `reg_number`.

2. **Empty-string field updates**: `RaceState._competitor` intentionally skips updating a field when the incoming value is an empty string, to avoid blanking data from an earlier `$A`/`$COMP` message.

3. **Qualifying vs race mode**: `_is_qualifying` is set on `$H` messages and cleared on `$G` messages. Snapshot sort order changes accordingly: `best_lap_time` ascending for qualifying, `position` numeric for race, `total_time` ascending under a purple flag.

4. **Speed calculation**: `last_lap_speed_mph` is computed only when both `track_length_miles` (from `$E TRACKLENGTH`) and a positive lap time are available. Zero or missing yields `None`.

5. **`$SP`/`$SR` are undocumented**: Real messages from some Orbits setups; carry per-lap position and time, map to `lap_info`.

6. **Protocol token quirks**: `_tokenize()` strips double-quotes and whitespace from every token. Flag strings in `$F` can have trailing spaces (`"Green "`), removed by `strip()`.

7. **No linter/formatter config**: Follow existing style — PEP 8, 4-space indent, double-quoted strings in tests, type hints on public functions.

8. **aiohttp: never reassign `AppKey` values after app startup**. Doing `app[some_key] = new_value` inside a request handler or background task triggers `DeprecationWarning: Changing state of started or joined application is deprecated`. Instead, store a mutable container (dict or set) under the key at creation time and mutate its contents:
   ```python
   # At app creation (fine):
   app[my_key] = {"value": None, "flag": False}
   # In a handler (fine — mutating the dict, not the key):
   app[my_key]["value"] = time.monotonic()
   # BAD — reassigning the key after startup:
   app[my_key] = time.monotonic()
   ```

9. **Server-side stateful features: initialise timestamps at startup, not lazily**. If a background watchdog or timeout compares `time.monotonic() - last_seen`, initialise `last_seen` to `time.monotonic()` in the app startup hook — not to `None`. A `None` check (`if last is not None`) means the condition never fires on a fresh server that has never received data. Similarly, when a new WebSocket client connects, `handle_ws` must immediately reflect the *current* server state — if the feed is already known to be lost (or timed-out), send the `no_feed` event right after the initial `full` message so the client doesn't display stale persisted data and wait for the next watchdog poll.

10. **Session mode detection has several non-obvious behaviours** — understand these before touching `_derive_session_mode`, `_is_qualifying`, or `_seen_race_info`:

    **Two-track detection**: There are two independent signals that must agree:
    - `_is_qualifying` (bool): set **by message type** — True when a `$H` (qual_info) arrives and `_seen_race_info` is still False; cleared to False on any `$G` (race_info). Controls sort order directly in `snapshot()`.
    - `session_mode` string label: derived by `_derive_session_mode()` via **substring matching** on `run_description` (from `$B`). Checks for `"practice"`, `"prac"`, `"familiarisation"`, `"qual"`.
    - `_is_qualifying=True` takes priority — `_derive_session_mode()` returns `"Qualifying"` immediately without reading the description.

    **Warm-up is now handled**: `"warm"` is in the practice keyword list, covering "Warm up", "Warm-up", "Warmup", etc. The full practice keyword list is: `"practice"`, `"prac"`, `"warm"`, `"familiarisation"`. Any description not matching these or `"qual"` falls through to `"Race"` if `_seen_race_info` or `run_description` is set — so always extend `_derive_session_mode()` when a new session type is needed.

    **`$H` during a race**: Some Orbits setups send `$H` (qual_info) during a race for best-lap tracking. The guard `if not self._seen_race_info` prevents `_is_qualifying` from being set and prevents `$H` positions from overwriting `$G` race positions. Best-lap fields (`best_lap_time`, `best_lap`) are always updated regardless.

    **Purple flag overrides sort regardless of mode**: Under a purple flag, `snapshot()` uses `_sort_key_purple` (total_time ascending) regardless of `_is_qualifying`. Intended for formation/pace laps at race end where cars are on track in order of total time.

    **String matching is spelling-sensitive**: `_derive_session_mode()` handles `"familiarisation"` (British English) but will silently return `"Race"` for any unrecognised description (e.g. `"Free Practice"`, `"Shakedown"`, `"Warm-up"`). Always check `_derive_session_mode()` when a new session type is needed.

11. **Dependencies belong in `requirements*.txt`, never hand-listed in a workflow's `pip install` line**: `.github/workflows/tests.yml` used to run `pip install pytest pytest-asyncio aiohttp` directly, so when `relay/gui.py`/`relay/env_config.py` started importing `platformdirs` (declared only in `relay/requirements-build.txt`), the test job's `pip install` step didn't pick it up and `tests/test_gui.py`/`tests/test_env_config.py` failed collection with `ModuleNotFoundError` — while `release.yml`/`publish.yml`, which already installed from `-r relay/requirements.txt -r relay/requirements-build.txt`, were unaffected. `tests.yml` now installs from `requirements-dev.txt` (pytest tooling) plus `relay/requirements.txt`, `relay/requirements-build.txt`, and `server/requirements.txt` — the same files the build/release workflows and local dev setup use. When adding a new import: add the package to the requirements file matching its scope (`relay/requirements.txt` for the headless runtime path, `relay/requirements-build.txt` for GUI/PyInstaller-only deps, `requirements-dev.txt` for test-only tooling) — don't add it to a workflow's inline `pip install` list, or the next workflow that doesn't happen to list it will silently drift out of sync.

