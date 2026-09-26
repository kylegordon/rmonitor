"""Entry point for the relay.

Connects to the on-premise rMonitor TCP feed, parses each message,
and forwards it to the server via HTTP POST.  Beside it, unless disabled, a
second source reads class codes from the timing host's port 51738 and
forwards them too; it is failure-isolated, so nothing on that port can stop
the rMonitor feed.

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
import pathlib
import sys
import threading
from dataclasses import dataclass

import aiohttp

from relay.class_code_client import ClassCodeClient, ClassCodeStatus
from relay.rmonitor_client import RMonitorClient

log = logging.getLogger("relay")

RMONITOR_HOST = os.environ.get("RMONITOR_HOST", "127.0.0.1")
RMONITOR_PORT = int(os.environ.get("RMONITOR_PORT", "50000"))
FEED_READ_TIMEOUT = float(os.environ.get("FEED_READ_TIMEOUT", "30.0"))
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8080")
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")
POST_TIMEOUT = float(os.environ.get("POST_TIMEOUT", "5.0"))
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "1.0"))
RETRY_MAX_DELAY = float(os.environ.get("RETRY_MAX_DELAY", "30.0"))
RETRY_MAX_ATTEMPTS = int(os.environ.get("RETRY_MAX_ATTEMPTS", "30"))
CLASS_CODES_ENABLED = os.environ.get("CLASS_CODES_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off"
)

#: The repo-root ``VERSION`` file: ``/opt/app/VERSION`` in the image, which copies it
#: beside the package, and the checkout's own copy everywhere else.
_VERSION_FILE = pathlib.Path(__file__).resolve().parents[1] / "VERSION"


def _read_release_version(*, version_file: pathlib.Path = _VERSION_FILE) -> str:
    """Return the release this relay was built from, e.g. ``"0.1.19"``.

    The ``VERSION`` file is read first; ``relay._version`` is the fallback for the
    PyInstaller builds, which bundle no ``VERSION`` file beside the package. The order
    matters: ``relay/_version.py`` is gitignored and written only by those builds, so a
    checkout can hold a stale copy, and reading it first would make the result depend
    on the machine rather than the commit.

    :returns: the stripped ``VERSION`` contents, else ``relay._version.CURRENT_VERSION``,
        else ``"0.0.0-dev"``.
    """
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        version = ""
    if version:
        return version
    try:
        from relay._version import CURRENT_VERSION
    except ImportError:
        return "0.0.0-dev"
    return CURRENT_VERSION


RELEASE_VERSION: str = _read_release_version()


@dataclass(frozen=True)
class RelayConfig:
    """Snapshot of the relay's tunable settings.

    Lets a caller (e.g. the GUI build) run the relay loop with values that
    didn't exist at process-import time, instead of the headless path's
    module-global-at-import-time approach.
    """

    host: str
    port: int
    feed_read_timeout: float
    server_url: str
    relay_secret: str
    post_timeout: float
    retry_delay: float
    retry_max_delay: float
    retry_max_attempts: int
    class_codes_enabled: bool = True

    @classmethod
    def from_env(cls) -> "RelayConfig":
        """Build a config from the current value of the module globals above."""
        return cls(
            host=RMONITOR_HOST,
            port=RMONITOR_PORT,
            feed_read_timeout=FEED_READ_TIMEOUT,
            server_url=SERVER_URL,
            relay_secret=RELAY_SECRET,
            post_timeout=POST_TIMEOUT,
            retry_delay=RETRY_DELAY,
            retry_max_delay=RETRY_MAX_DELAY,
            retry_max_attempts=RETRY_MAX_ATTEMPTS,
            class_codes_enabled=CLASS_CODES_ENABLED,
        )


# HTTP status codes that indicate a transient failure worth retrying.
_RETRIABLE = frozenset({429, 500, 502, 503, 504})


async def post_message(
    session: aiohttp.ClientSession,
    msg: dict,
    config: RelayConfig | None = None,
    *,
    on_attempt=None,
) -> bool:
    """POST a parsed message to the server, retrying on transient HTTP failures.

    Return whether the server accepted it.  Non-retriable responses (400,
    401, …) are logged and dropped so that a poison message cannot stall the
    relay indefinitely; the feed path ignores the result, while the class-code
    path treats ``False`` as a failed delivery and keeps the batch. Connection-level
    failures (DNS, refused connections, timeouts) are not retried here –
    they exit the process so the container restart policy can recover.

    Retriable HTTP failures (429/5xx) back off exponentially up to
    RETRY_MAX_DELAY. After RETRY_MAX_ATTEMPTS the relay gives up and exits
    the same way connection-level failures do, rather than blocking the
    TCP reader forever on a server outage that never resolves.

    `config` defaults to `RelayConfig.from_env()` when omitted (the headless
    Docker/console path); it is resolved here, at call time, so tests that
    monkeypatch the module globals above still take effect.
    """
    cfg = config if config is not None else RelayConfig.from_env()
    on_attempt = on_attempt if on_attempt is not None else lambda: None
    url = f"{cfg.server_url.rstrip('/')}/api/ingest"
    headers = {"Authorization": f"Bearer {cfg.relay_secret}"}
    timeout = aiohttp.ClientTimeout(total=cfg.post_timeout)
    attempt = 0
    while True:
        on_attempt()
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
                    return resp.status == 200
                attempt += 1
                if attempt >= cfg.retry_max_attempts:
                    log.error(
                        "Server still returning %s after %d attempts – "
                        "exiting so the container can restart",
                        resp.status,
                        attempt,
                    )
                    sys.exit(1)
                delay = min(
                    cfg.retry_delay * (2 ** (attempt - 1)), cfg.retry_max_delay
                )
                log.warning(
                    "Server returned %s – retrying in %.1fs (attempt %d/%d)",
                    resp.status,
                    delay,
                    attempt,
                    cfg.retry_max_attempts,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.error("POST failed: %s – exiting so the container can restart", exc)
            sys.exit(1)
        await asyncio.sleep(delay)


async def main(
    config: RelayConfig | None = None,
    *,
    on_feed_connect=None,
    on_feed_disconnect=None,
    on_feed_line=None,
    on_server_attempt=None,
    on_class_codes_status=None,
) -> None:
    """Run the relay loop: connect to the feed, POST each message to the server.

    Unless `config.class_codes_enabled` is false, a `ClassCodeClient` runs
    beside the feed as a task this function owns: it is cancelled when the
    feed loop ends, and it never raises into it.

    `config` defaults to `RelayConfig.from_env()` when omitted (the headless
    Docker/console path, unchanged); a caller that restarts the relay
    in-process (e.g. the GUI build's Save/Apply) passes an explicit config.

    The `on_*` callbacks are optional GUI-facing status hooks (default
    no-ops); the headless path never passes them.  `on_class_codes_status`
    receives each `ClassCodeStatus`, and one `"disabled"` status when class
    codes are switched off.
    """
    cfg = config if config is not None else RelayConfig.from_env()
    on_class_codes_status = (
        on_class_codes_status if on_class_codes_status is not None else lambda status: None
    )
    if not cfg.relay_secret:
        log.warning(
            "RELAY_SECRET is not set – ingest endpoint is unauthenticated"
        )
    log.info(
        "Starting relay %s – feed=%s:%s  server=%s",
        RELEASE_VERSION,
        cfg.host,
        cfg.port,
        cfg.server_url,
    )
    async with aiohttp.ClientSession() as http:
        async def on_message(msg: dict) -> None:
            await post_message(http, msg, cfg, on_attempt=on_server_attempt)

        async def on_class_codes(msg: dict) -> None:
            # post_message exits on a hard failure so the headless container
            # restarts, but a SystemExit leaving this sibling task would bypass
            # RelayRunner._run_with_respawn (see its docstring).  So it becomes
            # an ordinary error, on which ClassCodeClient keeps the batch and
            # retries it.  The :50000 path still exits on the same outage, so
            # nothing is masked.
            # A rejected POST (a 401 while the relay secret is being corrected,
            # say) is a failure too: pushes are not repeated, and the registry
            # is pulled again only on a reconnect.
            try:
                accepted = await post_message(http, msg, cfg, on_attempt=on_server_attempt)
            except SystemExit:
                accepted = False
            if not accepted:
                run = f" for run {msg['run_id']}" if msg.get("run_id") else ""
                raise ConnectionError(f"could not deliver {msg.get('type')}{run}")

        client = RMonitorClient(
            cfg.host,
            cfg.port,
            on_message,
            read_timeout=cfg.feed_read_timeout,
            on_connect=on_feed_connect,
            on_disconnect=on_feed_disconnect,
            on_raw_line=on_feed_line,
        )
        if not cfg.class_codes_enabled:
            log.info("Class codes are disabled")
            on_class_codes_status(ClassCodeStatus("disabled"))
            await client.run()
            return
        codes_client = ClassCodeClient(
            cfg.host, on_class_codes, on_status=on_class_codes_status
        )
        log.info("Reading class codes from %s:%s", codes_client.host, codes_client.port)
        codes = asyncio.create_task(codes_client.run())
        try:
            await client.run()
        finally:
            codes.cancel()
            await asyncio.gather(codes, return_exceptions=True)


class RelayRunner:
    """Runs `main()` as a cancellable task on a dedicated background thread's
    own event loop, so `restart()` can reconfigure the relay without tearing
    down the thread or relaunching the process.

    Tkinter's `mainloop()` and asyncio cannot share a thread, so this is what
    the GUI build uses to own the relay loop while the dialog runs on the
    main thread.
    """

    def __init__(
        self,
        *,
        on_feed_connect=None,
        on_feed_disconnect=None,
        on_feed_line=None,
        on_server_attempt=None,
        on_server_connect=None,
        on_server_disconnect=None,
        on_class_codes_status=None,
    ) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task | None = None
        self._on_feed_connect = on_feed_connect if on_feed_connect is not None else lambda: None
        self._on_feed_disconnect = (
            on_feed_disconnect if on_feed_disconnect is not None else lambda: None
        )
        self._on_feed_line = on_feed_line if on_feed_line is not None else lambda: None
        self._on_server_attempt = (
            on_server_attempt if on_server_attempt is not None else lambda: None
        )
        self._on_server_connect = (
            on_server_connect if on_server_connect is not None else lambda: None
        )
        self._on_server_disconnect = (
            on_server_disconnect if on_server_disconnect is not None else lambda: None
        )
        self._on_class_codes_status = (
            on_class_codes_status if on_class_codes_status is not None
            else lambda status: None
        )

    def start(self, config: RelayConfig) -> None:
        if self._thread is not None:
            raise RuntimeError("RelayRunner already started; use restart()")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="relay-runner", daemon=True
        )
        self._thread.start()
        asyncio.run_coroutine_threadsafe(
            self._replace_task(config), self._loop
        ).result(timeout=5.0)

    def restart(self, config: RelayConfig) -> None:
        if self._loop is None:
            raise RuntimeError("RelayRunner not started; call start() first")
        # Fire-and-forget: the caller (e.g. a GUI event handler) shouldn't
        # block on the old task's cancellation/cleanup.
        asyncio.run_coroutine_threadsafe(self._replace_task(config), self._loop)

    def run_coroutine(self, coro):
        """Schedule `coro` on this runner's loop/thread; e.g. for the GUI's
        update-check poll, which needs a loop to run on but isn't part of
        the relay task itself."""
        if self._loop is None:
            raise RuntimeError("RelayRunner not started; call start() first")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 5.0) -> None:
        if self._loop is None:
            return
        # Cancel the relay task and wait for it to finish first. Stopping
        # the loop is scheduled separately (call_soon_threadsafe) rather
        # than from inside the tracked coroutine itself: calling
        # loop.stop() there races run_coroutine_threadsafe's own
        # completion callback (it may see the loop stop and never run),
        # leaving this method's .result() waiting forever.
        asyncio.run_coroutine_threadsafe(self._cancel_task(), self._loop).result(
            timeout=timeout
        )
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        self._loop = None
        self._thread = None
        self._task = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _replace_task(self, config: RelayConfig) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Previous relay task ended while reconfiguring")
        self._task = asyncio.ensure_future(self._run_with_respawn(config))

    async def _run_with_respawn(self, config: RelayConfig) -> None:
        """Run `main(config)`, respawning it after any unhandled exception
        (including the `SystemExit` `post_message` raises on a hard server
        failure) instead of letting the background thread's task die
        silently. Catching inside this coroutine's own `try/except` keeps
        the exception from ever reaching `asyncio.tasks.Task.__step`'s
        `SystemExit`/`KeyboardInterrupt` special-case handling, which can
        otherwise unwind the loop before a done-callback observes it.
        """
        self._on_server_connect()
        while True:
            try:
                await main(
                    config,
                    on_feed_connect=self._on_feed_connect,
                    on_feed_disconnect=self._on_feed_disconnect,
                    on_feed_line=self._on_feed_line,
                    on_server_attempt=self._on_server_attempt,
                    on_class_codes_status=self._on_class_codes_status,
                )
                return
            except asyncio.CancelledError:
                raise
            except BaseException:
                log.exception("Relay task ended unexpectedly – respawning")
                self._on_server_disconnect()
                await asyncio.sleep(config.retry_delay)
                self._on_server_connect()

    async def _cancel_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Relay task ended with an error while stopping")
