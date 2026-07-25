"""Headless smoke test for relay/gui.py, run under xvfb-run.

Stubs RelayRunner (no real relay thread/TCP connection) and the
update-poll scheduling (no real background-loop dependency), so this only
exercises widget construction, default values, and notification visibility.
"""

import tkinter as tk

import pytest

from relay import env_config, gui


class FakeRunner:
    def __init__(self):
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


def test_on_save_persists_fields_and_restarts_runner_with_matching_config(app):
    app.host_var.set("10.0.0.5")
    app.port_var.set("12345")
    app.secret_var.set("s3cr3t")

    app._on_save()

    saved = env_config.load_env_file(app.env_path)
    assert saved["RMONITOR_HOST"] == "10.0.0.5"
    assert saved["RMONITOR_PORT"] == "12345"
    assert saved["RELAY_SECRET"] == "s3cr3t"

    assert app.runner.restarted_with.host == "10.0.0.5"
    assert app.runner.restarted_with.port == 12345
    assert app.runner.restarted_with.relay_secret == "s3cr3t"


def test_on_save_with_invalid_port_does_not_write_or_restart(app):
    app.port_var.set("not-a-number")

    app._on_save()

    assert env_config.load_env_file(app.env_path) == {}
    assert app.runner.restarted_with is None
