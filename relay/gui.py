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

_CONNECTED_COLOR = "green"
_DISCONNECTED_COLOR = "red"
_HEARTBEAT_IDLE_COLOR = "gray"
_HEARTBEAT_ACTIVE_COLOR = "red"
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
        self.runner.start(self._config_from_fields())
        self._poll_update()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TLabelframe", padding=8)
        style.configure("TLabelframe.Label", font=("TkDefaultFont", 10, "bold"))
        style.configure("TButton", padding=6)
        style.configure("TEntry", padding=2)

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)

        feed_frame = ttk.LabelFrame(frame, text="Feed")
        feed_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        feed_frame.columnconfigure(1, weight=1)

        ttk.Label(feed_frame, text="Feed IP:").grid(row=0, column=0, sticky="w")
        ttk.Entry(feed_frame, textvariable=self.host_var).grid(row=0, column=1, sticky="ew")
        self.feed_dot = ttk.Label(feed_frame, text="●", foreground=_DISCONNECTED_COLOR)
        self.feed_dot.grid(row=0, column=2, padx=(6, 0))
        self.feed_heart = ttk.Label(feed_frame, text="♥", foreground=_HEARTBEAT_IDLE_COLOR)
        self.feed_heart.grid(row=0, column=3, padx=(4, 0))

        ttk.Label(feed_frame, text="Feed port:").grid(row=1, column=0, sticky="w")
        ttk.Entry(feed_frame, textvariable=self.port_var).grid(row=1, column=1, sticky="ew")

        server_frame = ttk.LabelFrame(frame, text="Server")
        server_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        server_frame.columnconfigure(1, weight=1)

        ttk.Label(server_frame, text="Server URL:").grid(row=0, column=0, sticky="w")
        ttk.Entry(server_frame, textvariable=self.server_url_var).grid(row=0, column=1, sticky="ew")
        self.server_dot = ttk.Label(server_frame, text="●", foreground=_DISCONNECTED_COLOR)
        self.server_dot.grid(row=0, column=2, padx=(6, 0))
        self.server_heart = ttk.Label(server_frame, text="♥", foreground=_HEARTBEAT_IDLE_COLOR)
        self.server_heart.grid(row=0, column=3, padx=(4, 0))

        ttk.Label(server_frame, text="Relay secret:").grid(row=1, column=0, sticky="w")
        ttk.Entry(server_frame, textvariable=self.secret_var, show="*").grid(
            row=1, column=1, sticky="ew"
        )

        ttk.Button(frame, text="Save / Apply", command=self._on_save).grid(
            row=2, column=0, pady=(8, 0)
        )

        self.save_confirmation = ttk.Label(frame, text="Saved", foreground="green")
        self.save_confirmation.grid(row=3, column=0, pady=(4, 0))
        self.save_confirmation.grid_remove()

        self.update_label = ttk.Label(
            frame, textvariable=self.update_var, foreground="blue", cursor="hand2"
        )
        self.update_label.grid(row=4, column=0, pady=(8, 0))
        self.update_label.bind("<Button-1>", lambda _event: webbrowser.open(update_check.RELEASES_URL))
        self.update_label.grid_remove()

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.root.minsize(360, 260)

    def _set_feed_connected(self, connected: bool) -> None:
        self.feed_dot.configure(
            foreground=_CONNECTED_COLOR if connected else _DISCONNECTED_COLOR
        )

    def _set_server_connected(self, connected: bool) -> None:
        self.server_dot.configure(
            foreground=_CONNECTED_COLOR if connected else _DISCONNECTED_COLOR
        )

    def _pulse(self, label: ttk.Label) -> None:
        label.configure(foreground=_HEARTBEAT_ACTIVE_COLOR)
        self.root.after(
            _HEARTBEAT_PULSE_MS, lambda: label.configure(foreground=_HEARTBEAT_IDLE_COLOR)
        )

    def _on_feed_connect(self) -> None:
        self.root.after(0, self._set_feed_connected, True)

    def _on_feed_disconnect(self) -> None:
        self.root.after(0, self._set_feed_connected, False)

    def _on_feed_line(self) -> None:
        self.root.after(0, self._pulse, self.feed_heart)

    def _on_server_attempt(self) -> None:
        self.root.after(0, self._pulse, self.server_heart)

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
        self.runner.stop()
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
