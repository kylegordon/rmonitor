# Copilot Instructions for rmonitor

## Project Overview

`rmonitor` is a Python asyncio web application that connects to an **AMB rMonitor** TCP timing feed (as used by MyLaps Orbits and similar motorsport timing systems) and exposes a live-updating leaderboard in the browser via WebSocket.

## Technology Stack

- **Python 3.12** — all async/await patterns using `asyncio`
- **aiohttp ≥ 3.9, < 4** — the only runtime dependency (web server, WebSocket)
- **pytest** — for testing (not listed in `requirements.txt`; install separately with `pip install pytest`)
- **Docker / Docker Compose** — for containerised deployment
- No database, no ORM, no frontend build step

## Repository Layout

```
app/
├── main.py             # Entry point: wires up the TCP client and the web server
├── rmonitor_client.py  # Async TCP client + full rMonitor protocol parser
├── race_state.py       # In-memory race state, updated from parsed messages
├── server.py           # aiohttp routes: HTML page, /ws WebSocket, /api/state
└── templates/
    └── index.html      # Single-page HTML leaderboard (vanilla JS + WebSocket)
tests/
├── test_parser.py      # Unit tests for the protocol parser
└── test_race_state.py  # Unit tests for RaceState
examples/               # Sample AMB rMonitor capture files (real Sebring data)
captures/               # (runtime) destination for rmonitor_capture.py output
rmonitor_send.py        # Dev helper: TCP server that replays a sample file
rmonitor_capture.py     # Diagnostic tool: captures a live feed to a timestamped log
Dockerfile              # Production image (python:3.12-slim)
docker-compose.yml      # Compose file; optional `tunnel` profile adds cloudflared
requirements.txt        # Runtime deps only (aiohttp)
```

## Architecture

```
asyncio event loop
├── RMonitorClient.run()         # TCP → lines → parse_line() → on_message()
│     (auto-reconnects on error)
└── aiohttp web server
      ├── GET /                  → serves app/templates/index.html
      ├── GET /ws                → WebSocket (push-only; browser reconnects on drop)
      └── GET /api/state         → JSON snapshot of current race state
```

- **`RaceState`** is the single source of truth. It is mutated by `on_message()` in `main.py` and read by `server.py` for snapshot/broadcast.
- **`dirty` flag** on `RaceState` prevents redundant WebSocket broadcasts (only broadcast when state has actually changed).
- On a `$I` (init) message the full state is cleared and an `"init"` event is broadcast to all clients, causing the browser to re-render from scratch.
- The aiohttp `WebSocketResponse` uses a 30-second heartbeat to keep connections alive through Cloudflare's 100-second idle timeout.
- A `server_instance_id` UUID is sent to each WebSocket client on connect and with every broadcast, so clients can detect a server restart and reload.

## Protocol Messages

The parser in `rmonitor_client.py` handles these `$`-prefixed, comma-separated, double-quote-delimited lines:

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

Adding a new message type: register a parser function with the `@_reg("$X")` decorator in `rmonitor_client.py`, define a handler method on `RaceState`, and add it to `RaceState._HANDLERS`.

## Configuration (Environment Variables)

| Variable               | Default       | Description                          |
|------------------------|---------------|--------------------------------------|
| `RMONITOR_HOST`        | `127.0.0.1`   | rMonitor feed hostname or IP         |
| `RMONITOR_PORT`        | `50000`       | rMonitor feed TCP port               |
| `WEB_HOST`             | `0.0.0.0`     | Web server bind address              |
| `WEB_PORT`             | `8080`        | Web server port                      |
| `CAPTURE_DIR`          | `captures`    | Output directory for capture files   |
| `CLOUDFLARE_TUNNEL_TOKEN` | —          | Used only with the `tunnel` Compose profile |

## How to Run

### Install dependencies
```bash
pip install -r requirements.txt
```

### Run the web app
```bash
python -m app.main
# Browse to http://localhost:8080
```

### Test with sample data (no real timing hardware needed)
```bash
# Terminal 1 – replay Sebring sample data on port 50000
python rmonitor_send.py

# Terminal 2 – start the web app (defaults to 127.0.0.1:50000)
python -m app.main
```

`rmonitor_send.py` accepts optional positional args: `[FILE] [PORT]`.

### Capture a live feed
```bash
python rmonitor_capture.py   # writes timestamped log to captures/
```

### Run with Docker
```bash
export RMONITOR_HOST=192.168.10.24
docker compose up --build
# With Cloudflare Tunnel:
export CLOUDFLARE_TUNNEL_TOKEN=<token>
docker compose --profile tunnel up --build
```

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

- `tests/test_parser.py` — parses raw protocol strings and checks the resulting dict
- `tests/test_race_state.py` — feeds parsed message dicts into `RaceState` and checks resulting state and snapshots
- Tests are pure unit tests; no network, no aiohttp TestClient
- All tests pass with zero warnings on a clean `pip install -r requirements.txt && pip install pytest` install

## Common Pitfalls and Workarounds

1. **`reg_number` vs `number`**: `reg_number` is the registration key used internally (e.g. `"21"`). `number` is the displayed car number (may include letters, e.g. `"12X"`). Always key competitor dicts by `reg_number`.

2. **Empty-string field updates**: `RaceState._competitor` intentionally skips updating a field when the incoming value is an empty string, to avoid blanking out data already received from an earlier `$A` or `$COMP` message.

3. **Qualifying vs race mode**: `_is_qualifying` is set to `True` on `$H` messages and cleared back to `False` on `$G` messages. The snapshot sort order changes accordingly (`best_lap_time` ascending for qualifying, `position` numeric for race, `total_time` ascending under a purple flag).

4. **Speed calculation**: `last_lap_speed_mph` is computed only when both `track_length_miles` (from `$E TRACKLENGTH`) and a positive lap time are available. A zero or missing lap time yields `None`.

5. **`$SP`/`$SR` are undocumented**: These are real messages emitted by some Orbits setups; they carry per-lap position and time and map to the `lap_info` type.

6. **Protocol token quirks**: The tokeniser (`_tokenize`) strips double-quotes and surrounding whitespace from every token after splitting on commas. Flag strings in `$F` messages can have a trailing space (e.g., `"Green "`), which the `strip()` call removes.

7. **Shallow clone**: If you need git history or other branches, run `git fetch --unshallow origin` before any merge/rebase operation.

8. **No linter/formatter config present**: The project has no `pyproject.toml`, `setup.cfg`, `.flake8`, or similar. Follow the existing code style (PEP 8, 4-space indent, double-quoted strings in tests, type hints on public functions).
