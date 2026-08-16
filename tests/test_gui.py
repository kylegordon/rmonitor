"""Headless smoke test for relay/gui.py, run under xvfb-run.

Stubs RelayRunner (no real relay thread/TCP connection) and the
update-poll scheduling (no real background-loop dependency), so this only
exercises widget construction, default values, and notification visibility.
"""

import time
import tkinter as tk

import pytest
import ttkbootstrap as ttb

from relay import env_config, gui


class FakeRunner:
    def __init__(self, **_callbacks):
        self.started_with = None
        self.restarted_with = None

    def start(self, config):
        self.started_with = config

    def restart(self, config):
        self.restarted_with = config

    def run_coroutine(self, coro):
        coro.close()

    def stop(self, timeout=5.0):
        pass


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setattr(gui.env_config, "default_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(gui, "RelayRunner", FakeRunner)
    monkeypatch.setattr(gui.RelayGuiApp, "_poll_update", lambda self: None)

    # ttkbootstrap.Style is a process-wide singleton; without resetting it,
    # the second test in a session would reuse a Style bound to the previous
    # test's already-destroyed Tk root.
    monkeypatch.setattr(ttb.Style, "instance", None)

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available: {exc}")

    application = gui.RelayGuiApp(root)
    yield application
    application.root.destroy()


def test_fields_show_documented_defaults_when_no_env_file(app):
    assert app.host_var.get() == "127.0.0.1"
    assert app.port_var.get() == "50000"
    assert app.server_url_var.get() == "http://localhost:8080"
    assert app.secret_var.get() == ""


def test_update_notification_hidden_by_default(app):
    assert not app.update_label.winfo_ismapped()


def test_update_notification_becomes_visible_when_newer_version_found(app, monkeypatch):
    monkeypatch.setattr(gui.update_check, "is_newer", lambda remote, current: True)

    class FakeFuture:
        def result(self):
            return "99.0.0"

    app._on_version_result(FakeFuture())
    app.root.update_idletasks()

    assert app.update_label.winfo_ismapped()
    assert "99.0.0" in app.update_var.get()


def test_update_notification_stays_hidden_when_not_newer(app, monkeypatch):
    monkeypatch.setattr(gui.update_check, "is_newer", lambda remote, current: False)

    class FakeFuture:
        def result(self):
            return "0.0.1"

    app._on_version_result(FakeFuture())

    assert not app.update_label.winfo_ismapped()


def test_runner_start_is_deferred_until_mainloop_processes_events(app):
    # RelayRunner.start() spins up a background thread that touches Tkinter
    # almost immediately (on_server_connect). Calling it synchronously from
    # __init__, before root.mainloop() is running, races that thread against
    # the main thread and can crash with "main thread is not in main loop".
    # __init__ must only *schedule* the start via after(0, ...), not call it
    # directly, so it can't run until the event loop is confirmed pumping.
    assert app.runner.started_with is None

    app.root.update()

    assert app.runner.started_with is not None


def test_on_save_persists_fields_and_restarts_runner_with_matching_config(app):
    app.host_var.set("10.0.0.5")
    app.port_var.set("12345")
    app.server_url_var.set("https://example.com")
    app.secret_var.set("s3cr3t")

    app._on_save()

    saved = env_config.load_env_file(app.env_path)
    assert saved["RMONITOR_HOST"] == "10.0.0.5"
    assert saved["RMONITOR_PORT"] == "12345"
    assert saved["SERVER_URL"] == "https://example.com"
    assert saved["RELAY_SECRET"] == "s3cr3t"

    assert app.runner.restarted_with.host == "10.0.0.5"
    assert app.runner.restarted_with.port == 12345
    assert app.runner.restarted_with.server_url == "https://example.com"
    assert app.runner.restarted_with.relay_secret == "s3cr3t"


def test_on_save_with_invalid_port_does_not_write_or_restart(app):
    app.port_var.set("not-a-number")

    app._on_save()

    assert env_config.load_env_file(app.env_path) == {}
    assert app.runner.restarted_with is None


def _bootstyle_of(widget) -> str:
    # ttkbootstrap widgets don't expose the bootstyle keyword back via cget;
    # the applied color lives in the composed ttk style name instead
    # (e.g. "success.TButton").
    return str(widget.cget("style")).split(".")[0]


def test_set_feed_connected_toggles_status_button_style(app):
    app._set_feed_connected(True)
    assert _bootstyle_of(app.feed_status) == gui._CONNECTED_STYLE

    app._set_feed_connected(False)
    assert _bootstyle_of(app.feed_status) == gui._DISCONNECTED_STYLE


def test_set_server_connected_toggles_status_button_style(app):
    app._set_server_connected(True)
    assert _bootstyle_of(app.server_status) == gui._CONNECTED_STYLE

    app._set_server_connected(False)
    assert _bootstyle_of(app.server_status) == gui._DISCONNECTED_STYLE


def test_pulse_sets_active_then_reverts_to_current_state(app):
    app._set_feed_connected(True)

    app._pulse_feed()
    app.root.update_idletasks()
    assert _bootstyle_of(app.feed_status) == gui._PULSE_STYLE

    time.sleep((gui._HEARTBEAT_PULSE_MS / 1000) + 0.2)
    app.root.update()
    assert _bootstyle_of(app.feed_status) == gui._CONNECTED_STYLE


def test_save_confirmation_shows_then_hides_after_save(app):
    assert not app.save_confirmation.winfo_ismapped()

    app._on_save()
    app.root.update_idletasks()
    assert app.save_confirmation.winfo_ismapped()

    time.sleep((gui._SAVE_CONFIRMATION_MS / 1000) + 0.2)
    app.root.update()
    assert not app.save_confirmation.winfo_ismapped()


def test_on_close_destroys_window_even_if_runner_stop_times_out(app, monkeypatch):
    def raise_timeout(timeout=5.0):
        raise TimeoutError("did not stop in time")

    monkeypatch.setattr(app.runner, "stop", raise_timeout)
    destroyed = []
    monkeypatch.setattr(app.root, "destroy", lambda: destroyed.append(True))

    app.on_close()

    assert destroyed == [True]
