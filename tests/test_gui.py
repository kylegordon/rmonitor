"""Headless smoke test for relay/gui.py, run under xvfb-run.

Stubs RelayRunner (no real relay thread/TCP connection) and the
update-poll scheduling (no real background-loop dependency), so this only
exercises widget construction, default values, notification visibility,
window-geometry persistence, and the close/teardown path.
"""

import re
import importlib
import time
import tkinter as tk

import pytest
import ttkbootstrap as ttb

from relay import env_config, gui

# Tk's "WxH+X+Y" geometry string; the offsets may be negative, and Tk
# spells a negative one as either "-10" or "+-10" depending on platform.
_GEOMETRY_RE = r"\d+x\d+[+-]-?\d+[+-]-?\d+"


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
    try:
        application.root.destroy()
    except tk.TclError:
        pass  # a test (e.g. one exercising on_close()) may have already destroyed it


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
    real_destroy = app.root.destroy

    def fake_destroy():
        destroyed.append(True)
        real_destroy()

    monkeypatch.setattr(app.root, "destroy", fake_destroy)

    app.on_close()
    # _finish_close (and the real destroy() above) only runs once the
    # closing notice's after() timer fires – pump the loop past it rather
    # than asserting immediately.
    time.sleep((gui._CLOSING_NOTICE_MS / 1000) + 0.2)
    app.root.update()

    assert destroyed == [True]


def _launch_app(monkeypatch, tmp_path):
    """Build a RelayGuiApp against `tmp_path/.env` the way the `app` fixture
    does, but callable *after* a test has seeded that file – which the
    fixture, which constructs the app up front, can't offer."""
    monkeypatch.setattr(gui.env_config, "default_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(gui, "RelayRunner", FakeRunner)
    monkeypatch.setattr(gui.RelayGuiApp, "_poll_update", lambda self: None)
    monkeypatch.setattr(ttb.Style, "instance", None)

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display available: {exc}")

    return gui.RelayGuiApp(root)


def test_on_close_saves_current_window_geometry(app):
    app.root.update_idletasks()
    geometry = app.root.geometry()

    app.on_close()

    saved = env_config.load_env_file(app.env_path)
    assert saved["GUI_WINDOW_GEOMETRY"] == geometry


def test_saved_window_geometry_is_restored_on_next_launch(monkeypatch, tmp_path):
    env_config.save_env_file(
        tmp_path / ".env", {"GUI_WINDOW_GEOMETRY": "500x400+123+45"}
    )

    application = _launch_app(monkeypatch, tmp_path)
    application.root.update_idletasks()

    # Size only: a window manager is free to override a requested +x+y
    # placement, so asserting the full geometry string would be flaky
    # anywhere but the bare Xvfb server CI runs under.
    assert application.root.geometry().startswith("500x400")

    application.root.destroy()


def test_malformed_saved_geometry_is_ignored_rather_than_fatal(monkeypatch, tmp_path):
    # A hand-edited or truncated .env shouldn't stop the GUI coming up.
    env_config.save_env_file(
        tmp_path / ".env", {"GUI_WINDOW_GEOMETRY": "not-a-geometry"}
    )

    application = _launch_app(monkeypatch, tmp_path)
    application.root.update_idletasks()

    assert application.root.winfo_exists()
    assert re.fullmatch(_GEOMETRY_RE, application.root.geometry())

    application.root.destroy()


def test_missing_saved_geometry_leaves_tk_default_placement(app):
    # No GUI_WINDOW_GEOMETRY in the env file (the `app` fixture's tmp_path
    # starts empty) – tk should have sized and placed the window itself.
    app.root.update_idletasks()

    assert env_config.load_env_file(app.env_path).get("GUI_WINDOW_GEOMETRY") is None
    assert re.fullmatch(_GEOMETRY_RE, app.root.geometry())


def _pending_after_timers(root) -> set[str]:
    return set(root.tk.splitlist(root.tk.call("after", "info")))


def test_repeated_close_clicks_schedule_only_one_teardown(app, monkeypatch):
    # Clicking the window's X again during the closing notice used to redo
    # the geometry save and schedule a second _finish_close. That second
    # timer comes due after the first has destroyed the root, so Tk can't
    # dispatch it and dumps `invalid command name ...` to stderr on exit.
    saves = []
    real_save = gui.env_config.save_env_file

    def counting_save(path, updates):
        saves.append(updates)
        real_save(path, updates)

    monkeypatch.setattr(gui.env_config, "save_env_file", counting_save)
    before = _pending_after_timers(app.root)

    app.on_close()
    scheduled_by_first = _pending_after_timers(app.root) - before

    app.on_close()
    scheduled_by_both = _pending_after_timers(app.root) - before

    assert len(scheduled_by_first) == 1
    assert scheduled_by_both == scheduled_by_first
    assert len(saves) == 1


def test_unwritable_config_dir_does_not_block_window_close(app, monkeypatch):
    def raise_oserror(path, updates):
        raise OSError("read-only file system")

    monkeypatch.setattr(gui.env_config, "save_env_file", raise_oserror)

    app.on_close()
    app.root.update_idletasks()

    # The close sequence carried on past the failed geometry save rather
    # than letting the OSError escape the WM_DELETE_WINDOW handler.
    assert app.closing_notice.winfo_ismapped()


def test_import_migrates_the_legacy_env_file_before_reading_it(monkeypatch):
    # gui.py merges the `.env` into os.environ at import time, before
    # relay.main is imported. The migration has to land ahead of that read,
    # or an upgraded Windows install reads defaults from the new path while
    # the operator's real settings sit at the old one.
    calls = []
    real_load = env_config.load_env_file

    monkeypatch.setattr(
        env_config, "migrate_legacy_env_file", lambda: calls.append("migrate")
    )

    def recording_load(path):
        calls.append("load")
        return real_load(path)

    monkeypatch.setattr(env_config, "load_env_file", recording_load)

    importlib.reload(gui)

    assert calls[:2] == ["migrate", "load"]
