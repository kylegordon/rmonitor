# SMART Live Timing

A Python application that connects to an **AMB rMonitor** timing feed
(as used by MyLaps Orbits and similar systems) and displays a live-updating
leaderboard in the browser.

The application is split into two components that can each run in Docker:

| Component | Where it runs | Purpose |
|-----------|---------------|---------|
| **relay** | On-premise (same network as the timing system) | Connects to the rMonitor TCP feed, parses messages, forwards them to the server |
| **server** | Cloud / web server | Receives messages from the relay, maintains race state, serves the live leaderboard |

## Features

- Parses the full rMonitor protocol (`$F`, `$A`, `$COMP`, `$B`, `$C`, `$E`, `$G`, `$H`, `$I`, `$J`, `$SP`, `$SR`)
- Real-time HTML leaderboard via WebSocket — no polling, no page refresh
- Shows entrant names, numbers, positions, lap times, lap speeds, best laps
- Track name, race time, flag status and laps/time remaining in the header
- Automatic reconnect to the timing feed on connection loss
- Clears and restarts the display when a new session/race begins (`$I` init)
- State persisted to disk — server resumes after a restart

## Quick start

### With Docker Compose (both components on one machine)

```bash
# Required: shared secret between relay and server
export RELAY_SECRET=change-me

# Set the rMonitor feed host
export RMONITOR_HOST=192.168.10.24

docker compose up
```

Then open <http://localhost:8080>.

This pulls the latest pre-built images from GHCR
(`ghcr.io/kylegordon/rmonitor-relay` and `ghcr.io/kylegordon/rmonitor-server`).

#### Building from local source

To build the images yourself (e.g. for testing unreleased changes):

```bash
# Option A — build directly and run
docker build -f relay/Dockerfile -t ghcr.io/kylegordon/rmonitor-relay:latest .
docker build -f server/Dockerfile -t ghcr.io/kylegordon/rmonitor-server:latest .
docker compose up

# Option B — use a Compose override file (one-time setup)
cp docker-compose.override.yml.example docker-compose.override.yml
docker compose up --build   # now builds from source automatically
```

### Running components separately

**Server** (cloud):
```bash
cd server
pip install -r requirements.txt
export RELAY_SECRET=change-me
python -m server
```

**Relay** (on-premise):
```bash
cd relay
pip install -r requirements.txt
export RMONITOR_HOST=192.168.10.24
export SERVER_URL=https://your-server.example.com
export RELAY_SECRET=change-me
python -m relay
```

## Configuration

### Relay

| Variable | Default | Description |
|---|---|---|
| `RMONITOR_HOST` | `127.0.0.1` | rMonitor feed hostname or IP |
| `RMONITOR_PORT` | `50000` | rMonitor feed TCP port |
| `SERVER_URL` | `http://localhost:8080` | Base URL of the server |
| `RELAY_SECRET` | *(empty)* | Shared key sent as `Authorization: Bearer` header |
| `POST_TIMEOUT` | `5.0` | HTTP POST timeout in seconds |
| `RETRY_DELAY` | `1.0` | Delay between retries on transient failure |

### Server

| Variable | Default | Description |
|---|---|---|
| `RELAY_SECRET` | *(empty)* | Must match the relay's value; disables auth if empty |
| `WEB_HOST` | `0.0.0.0` | Web server bind address |
| `WEB_PORT` | `8080` | Web server port |
| `STATE_FILE` | `data/state.json` | Where to persist race state |
| `SAVE_INTERVAL` | `10` | How often (seconds) to persist state |
| `BROADCAST_INTERVAL` | `0.25` | WebSocket push interval (seconds) |

## Communication

The relay authenticates each POST with `Authorization: Bearer <RELAY_SECRET>`.
No encryption is applied to the message body (use HTTPS / a tunnel for transport
security). Each rMonitor message is sent as a single JSON object to `POST /api/ingest`.

## Protocol compatibility

> **Note:** The `$A` and `$COMP` competitor records include a `nationality`
> field, but in practice many timing operators use it to carry the **vehicle
> make/model** (e.g. "Porsche 911 GT3 RSR", "Mini Cooper S"). The web
> interface displays this as a tooltip when hovering over a driver's name.

The parser follows the same protocol specification as:

- [only-entertainment/rmonitor](https://github.com/only-entertainment/rmonitor) (Python)
- [zacharyfox/RMonitorLeaderboard](https://github.com/zacharyfox/RMonitorLeaderboard) (Java)

Based on:
- [AMB RMonitor Timing Protocol](http://www.imsatiming.com/software/protocols/AMB%20RMonitor%20Timing%20Protocol.pdf)
- [IMSA Enhanced RMon Timing Protocol](http://www.imsatiming.com/software/protocols/IMSA%20Enhanced%20RMon%20Timing%20Protocol%20v1.03.pdf)

## Cloudflare Tunnel

The server works behind a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/). The JavaScript client automatically uses `wss://` when the page is served over HTTPS, and the server sends WebSocket ping frames every 30 seconds to keep connections alive through Cloudflare's 100-second idle timeout.

```bash
export RMONITOR_HOST=192.168.10.24
export RELAY_SECRET=change-me
export CLOUDFLARE_TUNNEL_TOKEN=<your-token>

docker compose --profile tunnel up
```

Configure the tunnel to point to `http://server:8080`.

## Testing with sample data

```bash
# Terminal 1: start the server (defaults to 127.0.0.1:50000 for relay)
export RELAY_SECRET=dev
python -m server

# Terminal 2: run the relay locally
export RELAY_SECRET=dev
python -m relay

# Terminal 3: replay sample data
python rmonitor_send.py
```

## Running tests

```bash
pip install pytest pytest-asyncio aiohttp
python3 -m pytest tests/ -v
```

## Architecture

```
relay/
├── rmonitor_client.py  # Async TCP client and protocol parser
└── main.py             # Entry point — connects to feed, POSTs to server

server/
├── race_state.py       # In-memory race state
├── state_store.py      # StateStore ABC + JsonFileStateStore
├── server.py           # aiohttp web server (WebSocket + /api/ingest)
├── main.py             # Entry point — starts web server
└── templates/
    └── index.html      # Live-updating HTML leaderboard
```

### Future Lambda deployment

`StateStore` is an abstract interface. To deploy the server on AWS Lambda:
1. Implement `DynamoStateStore` (or `RedisStateStore`) replacing `JsonFileStateStore`
2. Use API Gateway WebSocket API instead of the in-process aiohttp WebSocket handler
3. Deploy `server/` as Lambda functions

No other server code needs to change.

## License

See existing repository licence.

