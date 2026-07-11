"""Entry point for the relay.

Connects to the on-premise rMonitor TCP feed, parses each message,
and forwards it to the server via HTTP POST.

Messages are delivered in order: each POST completes (or retries) before
the next TCP message is read, preserving protocol ordering at the cost of
blocking the TCP reader during transient server outages.

Connection-level failures (DNS resolution, refused connections, timeouts)
are not retried in-process: the relay exits and relies on the container's
`restart: unless-stopped` policy to come back up with a fresh event loop,
connector, and resolver state rather than spinning in a stuck retry loop.
"""

import asyncio
import logging
import os
import sys

import aiohttp

from relay.rmonitor_client import RMonitorClient

log = logging.getLogger("relay")

RMONITOR_HOST = os.environ.get("RMONITOR_HOST", "127.0.0.1")
RMONITOR_PORT = int(os.environ.get("RMONITOR_PORT", "50000"))
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8080")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")
POST_TIMEOUT = float(os.environ.get("POST_TIMEOUT", "5.0"))
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "1.0"))

# HTTP status codes that indicate a transient failure worth retrying.
_RETRIABLE = frozenset({429, 500, 502, 503, 504})


async def post_message(session: aiohttp.ClientSession, msg: dict) -> None:
    """POST a parsed message to the server, retrying on transient HTTP failures.

    Non-retriable responses (400, 401, …) are logged and dropped so that
    a poison message cannot stall the relay indefinitely. Connection-level
    failures (DNS, refused connections, timeouts) are not retried here –
    they exit the process so the container restart policy can recover.
    """
    url = f"{SERVER_URL.rstrip('/')}/api/ingest"
    headers = {"Authorization": f"Bearer {RELAY_SECRET}"}
    timeout = aiohttp.ClientTimeout(total=POST_TIMEOUT)
    while True:
        try:
            async with session.post(
                url, json=msg, headers=headers, timeout=timeout
            ) as resp:
                if resp.status not in _RETRIABLE:
                    if resp.status != 200:
                        log.warning(
                            "Server returned non-retriable %s for '%s' – dropping",
                            resp.status,
                            msg.get("type"),
                        )
                    return
                log.warning(
                    "Server returned %s – retrying in %ss", resp.status, RETRY_DELAY
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("POST failed: %s – exiting so the container can restart", exc)
            sys.exit(1)
        await asyncio.sleep(RETRY_DELAY)


async def main() -> None:
    if not RELAY_SECRET:
        log.warning(
            "RELAY_SECRET is not set – ingest endpoint is unauthenticated"
        )
    log.info(
        "Starting relay – feed=%s:%s  server=%s",
        RMONITOR_HOST,
        RMONITOR_PORT,
        SERVER_URL,
    )
    async with aiohttp.ClientSession() as http:
        async def on_message(msg: dict) -> None:
            await post_message(http, msg)

        client = RMonitorClient(RMONITOR_HOST, RMONITOR_PORT, on_message)
        await client.run()
