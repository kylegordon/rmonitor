"""GUI entry point for the relay's standalone (PyInstaller) build.

Provides a small Tkinter dialog (feed IP, feed port, server URL, relay
secret, Save/Apply) that persists to an `.env` file and reconfigures the
relay's TCP-to-HTTP forwarding loop in-process, without relaunching the
process.
Also polls the repo's `VERSION` file on startup and every 6 hours, showing
a notification with a link to the GitHub Releases page when a newer
version is available (notify-only, no auto-update).

Tkinter's `mainloop()` runs on the main thread; the relay loop runs on a
background thread via `RelayRunner` (Tkinter and asyncio cannot share a
thread). Any code touching Tkinter widgets from a callback that fires on
the runner's thread must marshal back via `root.after(...)`.
"""

import asyncio
import logging
import os
import tkinter as tk
import webbrowser
from tkinter import ttk

import ttkbootstrap as ttb

# The `.env` file must be merged into os.environ *before* relay.main is
# imported, since RelayConfig.from_env() reads module globals that
# relay.main fixes at import time from os.environ. This is how advanced
# .env-only values (FEED_READ_TIMEOUT, etc.) reach the GUI build without a
# dedicated dialog field.
from relay import env_config

os.environ.update(env_config.load_env_file(env_config.default_env_path()))

from dataclasses import replace  # noqa: E402 – after the os.environ merge above

from relay import update_check  # noqa: E402
from relay.main import RelayConfig, RelayRunner, main as relay_main  # noqa: E402, F401

try:
    from relay._version import CURRENT_VERSION
except ImportError:
    CURRENT_VERSION = "0.0.0-dev"

log = logging.getLogger("relay.gui")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = "50000"
_DEFAULT_SERVER_URL = "http://localhost:8080"
_UPDATE_POLL_INTERVAL_MS = 6 * 60 * 60 * 1000

_THEME = "bootstrap-light"
_CONNECTED_STYLE = "success"
_DISCONNECTED_STYLE = "danger"
_PULSE_STYLE = "info"
_HEARTBEAT_PULSE_MS = 400
_SAVE_CONFIRMATION_MS = 1500


class RelayGuiApp:
    """Tkinter config dialog: feed IP/port/secret fields backed by the
    `.env` file at `env_config.default_env_path()`, wired to a `RelayRunner`
    so Save/Apply reconfigures the running relay without relaunching, plus
    a polled update-available notification.
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.env_path = env_config.default_env_path()
        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.server_url_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        self.update_var = tk.StringVar(value="")
        self._feed_connected = False
        self._server_connected = False

        self._configure_style()
        self._build_widgets()
        self._load_initial_values()

        self.runner = RelayRunner(
            on_feed_connect=self._on_feed_connect,
            on_feed_disconnect=self._on_feed_disconnect,
            on_feed_line=self._on_feed_line,
            on_server_attempt=self._on_server_attempt,
            on_server_connect=self._on_server_connect,
            on_server_disconnect=self._on_server_disconnect,
        )
        # Deferred via `after(0, ...)` rather than called directly: start()
        # spins up the runner's background thread, which calls
        # on_server_connect() (touching Tkinter via root.after()) almost
        # immediately – before any real network I/O. If that races ahead of
        # this constructor returning to main_gui()'s root.mainloop() call,
        # Tkinter raises "main thread is not in main loop" from the
        # background thread, which isn't caught anywhere in
        # RelayRunner._run_with_respawn and silently kills the relay task
        # before it ever attempts the feed connection. Scheduling start()
        # through after(0, ...) guarantees mainloop is already pumping
        # events by the time it (and the thread it spawns) runs.
        self.root.after(0, self._start_runner)

    def _start_runner(self) -> None:
        self.runner.start(self._config_from_fields())
        self._poll_update()

    def _configure_style(self) -> None:
        self.style = ttb.Style(theme=_THEME)
        self.style.configure("TLabelframe", padding=8)
        self.style.configure("TLabelframe.Label", font=("TkDefaultFont", 10, "bold"))
        self.style.configure("TEntry", padding=2)

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        self.feed_status = ttb.Button(
            frame, text="RMonitor Source", bootstyle=self._feed_style()
        )
        self.feed_status.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 8))

        self.server_status = ttb.Button(
            frame, text="Feed Destination", bootstyle=self._server_style()
        )
        self.server_status.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 8))

        feed_frame = ttk.LabelFrame(frame, text="Feed")
        feed_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        feed_frame.columnconfigure(1, weight=1)

        ttk.Label(feed_frame, text="Feed IP:").grid(row=0, column=0, sticky="w")
        ttk.Entry(feed_frame, textvariable=self.host_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(feed_frame, text="Feed port:").grid(row=1, column=0, sticky="w")
        ttk.Entry(feed_frame, textvariable=self.port_var).grid(row=1, column=1, sticky="ew")

        server_frame = ttk.LabelFrame(frame, text="Server")
        server_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        server_frame.columnconfigure(1, weight=1)

        ttk.Label(server_frame, text="Server URL:").grid(row=0, column=0, sticky="w")
        ttk.Entry(server_frame, textvariable=self.server_url_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(server_frame, text="Relay secret:").grid(row=1, column=0, sticky="w")
        ttk.Entry(server_frame, textvariable=self.secret_var, show="*").grid(
            row=1, column=1, sticky="ew"
        )

        ttb.Button(frame, text="Save / Apply", bootstyle="primary", command=self._on_save).grid(
            row=3, column=0, columnspan=2, pady=(8, 0)
        )

        self.save_confirmation = ttk.Label(frame, text="Saved", foreground="green")
        self.save_confirmation.grid(row=4, column=0, columnspan=2, pady=(4, 0))
        self.save_confirmation.grid_remove()

        self.update_label = ttk.Label(
            frame, textvariable=self.update_var, foreground="blue", cursor="hand2"
        )
        self.update_label.grid(row=5, column=0, columnspan=2, pady=(8, 0))
        self.update_label.bind("<Button-1>", lambda _event: webbrowser.open(update_check.RELEASES_URL))
        self.update_label.grid_remove()

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.minsize(380, 300)

    def _feed_style(self) -> str:
        return _CONNECTED_STYLE if self._feed_connected else _DISCONNECTED_STYLE

    def _server_style(self) -> str:
        return _CONNECTED_STYLE if self._server_connected else _DISCONNECTED_STYLE

    def _set_feed_connected(self, connected: bool) -> None:
        self._feed_connected = connected
        self.feed_status.configure(bootstyle=self._feed_style())

    def _set_server_connected(self, connected: bool) -> None:
        self._server_connected = connected
        self.server_status.configure(bootstyle=self._server_style())

    def _pulse_feed(self) -> None:
        self.feed_status.configure(bootstyle=_PULSE_STYLE)
        self.root.after(
            _HEARTBEAT_PULSE_MS,
            lambda: self.feed_status.configure(bootstyle=self._feed_style()),
        )

    def _pulse_server(self) -> None:
        self.server_status.configure(bootstyle=_PULSE_STYLE)
        self.root.after(
            _HEARTBEAT_PULSE_MS,
            lambda: self.server_status.configure(bootstyle=self._server_style()),
        )

    def _on_feed_connect(self) -> None:
        self.root.after(0, self._set_feed_connected, True)

    def _on_feed_disconnect(self) -> None:
        self.root.after(0, self._set_feed_connected, False)

    def _on_feed_line(self) -> None:
        self.root.after(0, self._pulse_feed)

    def _on_server_attempt(self) -> None:
        self.root.after(0, self._pulse_server)

    def _on_server_connect(self) -> None:
        self.root.after(0, self._set_server_connected, True)

    def _on_server_disconnect(self) -> None:
        self.root.after(0, self._set_server_connected, False)

    def _load_initial_values(self) -> None:
        values = env_config.load_env_file(self.env_path)
        self.host_var.set(values.get("RMONITOR_HOST", _DEFAULT_HOST))
        self.port_var.set(values.get("RMONITOR_PORT", _DEFAULT_PORT))
        self.server_url_var.set(values.get("SERVER_URL", _DEFAULT_SERVER_URL))
        self.secret_var.set(values.get("RELAY_SECRET", ""))

    def _config_from_fields(self) -> RelayConfig:
        return replace(
            RelayConfig.from_env(),
            host=self.host_var.get(),
            port=int(self.port_var.get()),
            server_url=self.server_url_var.get(),
            relay_secret=self.secret_var.get(),
        )

    def _on_save(self) -> None:
        try:
            config = self._config_from_fields()
        except ValueError:
            log.error("Invalid feed port %r – not saved", self.port_var.get())
            return
        env_config.save_env_file(
            self.env_path,
            {
                "RMONITOR_HOST": self.host_var.get(),
                "RMONITOR_PORT": self.port_var.get(),
                "SERVER_URL": self.server_url_var.get(),
                "RELAY_SECRET": self.secret_var.get(),
            },
        )
        self.runner.restart(config)
        self.save_confirmation.grid()
        self.root.after(_SAVE_CONFIRMATION_MS, self.save_confirmation.grid_remove)

    def _poll_update(self) -> None:
        future = self.runner.run_coroutine(update_check.fetch_latest_version())
        future.add_done_callback(
            lambda fut: self.root.after(0, self._on_version_result, fut)
        )
        self.root.after(_UPDATE_POLL_INTERVAL_MS, self._poll_update)

    def _on_version_result(self, future) -> None:
        try:
            latest = future.result()
            newer = update_check.is_newer(latest, CURRENT_VERSION)
        except Exception as exc:
            log.warning("Update check failed: %s", exc)
            return
        if newer:
            self.update_var.set(f"Update available: v{latest} (click to download)")
            self.update_label.grid()
        else:
            self.update_var.set("")
            self.update_label.grid_remove()

    def on_close(self) -> None:
        # RelayRunner.stop() waits on a deadline for the background task to
        # finish cancelling and raises TimeoutError if a graceful shutdown
        # (e.g. an in-flight HTTPS POST) runs long. The runner thread is a
        # daemon, so it's torn down with the process regardless – the
        # window closing shouldn't be held hostage by that wait.
        try:
            self.runner.stop()
        except TimeoutError:
            log.warning("Relay runner did not stop cleanly within its timeout")
        self.root.destroy()


def main_gui() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    root = tk.Tk()
    root.title("rMonitor Relay")
    app = RelayGuiApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main_gui()
