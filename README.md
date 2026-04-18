# rMonitor Live Timing Display

A Python web application that connects to an **AMB rMonitor** timing feed
(as used by MyLaps Orbits and similar systems) and displays a live-updating
leaderboard in the browser.

## Features

- Connects to any rMonitor TCP feed and parses the full protocol
  (`$F`, `$A`, `$COMP`, `$B`, `$C`, `$E`, `$G`, `$H`, `$I`, `$J`, `$SP`, `$SR`)
- Real-time HTML leaderboard via WebSocket — no polling, no page refresh
- Shows entrant names, numbers, positions, lap times, lap speeds, best laps
- Track name, race time, flag status and laps/time remaining in the header
- Automatic reconnect to the timing feed on connection loss
- Clears and restarts the display when a new session/race begins (`$I` init)
- Runs as a lightweight Python server — ideal for Docker deployment

## Protocol compatibility

The parser follows the same protocol specification as:

- [only-entertainment/rmonitor](https://github.com/only-entertainment/rmonitor) (Python)
- [zacharyfox/RMonitorLeaderboard](https://github.com/zacharyfox/RMonitorLeaderboard) (Java)

Based on:
- [AMB RMonitor Timing Protocol](http://www.imsatiming.com/software/protocols/AMB%20RMonitor%20Timing%20Protocol.pdf)
- [IMSA Enhanced RMon Timing Protocol](http://www.imsatiming.com/software/protocols/IMSA%20Enhanced%20RMon%20Timing%20Protocol%20v1.03.pdf)

## Quick start

### With Docker (recommended)

```bash
# Set the rMonitor feed host and port
export RMONITOR_HOST=192.168.10.24
export RMONITOR_PORT=50000

docker compose up --build
```

Then open <http://localhost:8080>.

### Without Docker

```bash
pip install -r requirements.txt

# Configure via environment variables
export RMONITOR_HOST=192.168.10.24
export RMONITOR_PORT=50000

python -m app.main
```

Open <http://localhost:8080>.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `RMONITOR_HOST` | `127.0.0.1` | rMonitor feed hostname or IP |
| `RMONITOR_PORT` | `50000` | rMonitor feed TCP port |
| `WEB_HOST` | `0.0.0.0` | Web server bind address |
| `WEB_PORT` | `8080` | Web server port |

## Cloudflare Tunnel

The app works behind a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) without any extra configuration. The JavaScript client automatically uses `wss://` when the page is served over HTTPS, and the server sends WebSocket ping frames every 30 seconds to keep connections alive through Cloudflare's 100-second idle timeout.

### Quick setup with Docker Compose

1. Create a tunnel in the [Zero Trust dashboard](https://one.dash.cloudflare.com/) and copy the tunnel token.
2. Point the tunnel's public hostname to `http://rmonitor:8080` (the Docker service name).
3. Start both services:

```bash
export RMONITOR_HOST=192.168.10.24
export CLOUDFLARE_TUNNEL_TOKEN=<your-token-here>

docker compose --profile tunnel up --build
```

The app will be available at your configured public hostname over HTTPS.

### Without Docker

Install and run `cloudflared` manually:

```bash
# Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel --no-autoupdate run --token <your-token-here>
```

Configure the tunnel's ingress rule to point to `http://localhost:8080`.

## Testing with sample data

A test sender script replays the bundled Sebring sample data:

```bash
# Terminal 1: start the test sender
python rmonitor_send.py

# Terminal 2: start the web app (defaults to 127.0.0.1:50000)
python -m app.main
```

## Running tests

```bash
pip install pytest
python -m pytest tests/ -v
```

## Architecture

```
app/
├── main.py             # Entry point — starts TCP client + web server
├── rmonitor_client.py  # Async TCP client and protocol parser
├── race_state.py       # In-memory race state management
├── server.py           # aiohttp web server with WebSocket
└── templates/
    └── index.html      # Live-updating HTML leaderboard
```

## License

See existing repository licence.
