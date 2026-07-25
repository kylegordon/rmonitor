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


class RelayGuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.runner = RelayRunner()
        self.env_path = env_config.default_env_path()
        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.server_url_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        self.update_var = tk.StringVar(value="")

        self._build_widgets()
        self._load_initial_values()

        self.runner.start(self._config_from_fields())
        self._poll_update()

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frame, text="Feed IP:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.host_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(frame, text="Feed port:").grid(row=1, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.port_var).grid(row=1, column=1, sticky="ew")

        ttk.Label(frame, text="Server URL:").grid(row=2, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.server_url_var).grid(row=2, column=1, sticky="ew")

        ttk.Label(frame, text="Relay secret:").grid(row=3, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.secret_var, show="*").grid(
            row=3, column=1, sticky="ew"
        )

        ttk.Button(frame, text="Save / Apply", command=self._on_save).grid(
            row=4, column=0, columnspan=2, pady=(8, 0)
        )

        self.update_label = ttk.Label(
            frame, textvariable=self.update_var, foreground="blue", cursor="hand2"
        )
        self.update_label.grid(row=5, column=0, columnspan=2, pady=(8, 0))
        self.update_label.bind("<Button-1>", lambda _event: webbrowser.open(update_check.RELEASES_URL))
        self.update_label.grid_remove()

        frame.columnconfigure(1, weight=1)

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
