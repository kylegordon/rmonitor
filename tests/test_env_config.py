"""Tests for relay/env_config.py's `.env` reader/writer and path resolution."""

import pytest

from relay import env_config

# Values the real Windows backend would get back from the Win32 API.
# Forward slashes keep the joined result comparable on a Linux runner.
_FAKE_WIN_FOLDERS = {
    "CSIDL_APPDATA": "C:/Users/test/AppData/Roaming",
    "CSIDL_LOCAL_APPDATA": "C:/Users/test/AppData/Local",
}


@pytest.fixture
def on_windows(monkeypatch):
    """Route env_config's platformdirs lookup through the Windows backend.

    The defect these tests guard against is structurally invisible on
    Linux: the Unix backend ignores `appauthor` and `roaming` entirely, so
    only the Windows backend's path composition can show a Local-vs-Roaming
    or doubled-segment regression.
    """
    from platformdirs import windows

    monkeypatch.setattr(
        windows, "get_win_folder", lambda const: _FAKE_WIN_FOLDERS[const]
    )

    def as_windows(appname, appauthor=None, roaming=False, **_kwargs):
        return windows.Windows(
            appname=appname, appauthor=appauthor, roaming=roaming
        ).user_config_dir

    monkeypatch.setattr(env_config.platformdirs, "user_config_dir", as_windows)


@pytest.fixture
def paths(monkeypatch, tmp_path):
    """The current and legacy `.env` locations, pointed at tmp_path."""
    current, legacy = tmp_path / "new" / ".env", tmp_path / "old" / ".env"
    monkeypatch.setattr(env_config, "default_env_path", lambda: current)
    monkeypatch.setattr(env_config, "legacy_env_path", lambda: legacy)
    return current, legacy


def test_default_env_path_ends_in_app_dir_and_dotenv():
    path = env_config.default_env_path()
    assert path.name == ".env"
    assert path.parent.name == "rmonitor-relay"


def test_load_env_file_on_nonexistent_path_returns_empty_dict(tmp_path):
    assert env_config.load_env_file(tmp_path / "does-not-exist" / ".env") == {}


def test_save_then_load_round_trips_known_keys(tmp_path):
    path = tmp_path / ".env"
    env_config.save_env_file(
        path,
        {
            "RMONITOR_HOST": "192.168.10.24",
            "RMONITOR_PORT": "50000",
            "RELAY_SECRET": "s3cr3t",
            "SERVER_URL": "https://example.com",
        },
    )
    loaded = env_config.load_env_file(path)
    assert loaded["RMONITOR_HOST"] == "192.168.10.24"
    assert loaded["RMONITOR_PORT"] == "50000"
    assert loaded["RELAY_SECRET"] == "s3cr3t"
    assert loaded["SERVER_URL"] == "https://example.com"


def test_save_env_file_preserves_unknown_hand_added_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("FEED_READ_TIMEOUT=15\nRMONITOR_HOST=127.0.0.1\n")

    env_config.save_env_file(path, {"RMONITOR_HOST": "10.0.0.5"})

    loaded = env_config.load_env_file(path)
    assert loaded["FEED_READ_TIMEOUT"] == "15"
    assert loaded["RMONITOR_HOST"] == "10.0.0.5"


def test_save_env_file_preserves_comments_and_blank_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# a header comment\n\nRMONITOR_HOST=127.0.0.1\n")

    env_config.save_env_file(path, {"RMONITOR_HOST": "10.0.0.5"})

    text = path.read_text()
    assert "# a header comment" in text
    assert "RMONITOR_HOST=10.0.0.5" in text


def test_windows_env_path_is_roaming_appdata_without_a_doubled_segment(on_windows):
    # README documents `%APPDATA%\rmonitor-relay\.env`. platformdirs'
    # defaults would give Local AppData with the appname repeated as the
    # author directory instead.
    assert (
        str(env_config.default_env_path())
        == "C:/Users/test/AppData/Roaming/rmonitor-relay/.env"
    )


def test_windows_legacy_path_is_where_releases_up_to_0_1_13_wrote_the_file(on_windows):
    assert (
        str(env_config.legacy_env_path())
        == "C:/Users/test/AppData/Local/rmonitor-relay/rmonitor-relay/.env"
    )


def test_migration_moves_legacy_file_when_no_current_file_exists(paths):
    current, legacy = paths
    legacy.parent.mkdir()
    legacy.write_text("RMONITOR_HOST=192.168.10.24\nRELAY_SECRET=s3cr3t\n")

    env_config.migrate_legacy_env_file()

    loaded = env_config.load_env_file(current)
    assert loaded["RMONITOR_HOST"] == "192.168.10.24"
    assert loaded["RELAY_SECRET"] == "s3cr3t"
    assert not legacy.exists()


def test_migration_leaves_an_existing_current_file_untouched(paths):
    current, legacy = paths
    current.parent.mkdir()
    current.write_text("RMONITOR_HOST=10.0.0.5\n")
    legacy.parent.mkdir()
    legacy.write_text("RMONITOR_HOST=192.168.10.24\n")

    env_config.migrate_legacy_env_file()

    assert env_config.load_env_file(current)["RMONITOR_HOST"] == "10.0.0.5"
    assert legacy.exists()  # not clobbered, and not discarded either


def test_migration_is_a_no_op_when_there_is_nothing_to_migrate(paths):
    current, _legacy = paths

    env_config.migrate_legacy_env_file()

    assert not current.exists()


def test_migration_does_not_move_the_file_onto_itself(monkeypatch, tmp_path):
    # On Linux both helpers resolve to the same XDG path, since platformdirs'
    # Unix backend ignores the two parameters that differ between them.
    path = tmp_path / ".env"
    path.write_text("RMONITOR_HOST=10.0.0.5\n")
    monkeypatch.setattr(env_config, "default_env_path", lambda: path)
    monkeypatch.setattr(env_config, "legacy_env_path", lambda: path)

    env_config.migrate_legacy_env_file()

    assert env_config.load_env_file(path)["RMONITOR_HOST"] == "10.0.0.5"


def test_migration_failure_is_logged_rather_than_fatal(paths, monkeypatch, caplog):
    current, legacy = paths
    legacy.parent.mkdir()
    legacy.write_text("RMONITOR_HOST=10.0.0.5\n")

    def raise_oserror(src, dst):
        raise OSError("permission denied")

    monkeypatch.setattr(env_config.shutil, "move", raise_oserror)

    env_config.migrate_legacy_env_file()

    # The old file is still readable by hand, and startup carried on.
    assert legacy.exists()
    assert not current.exists()
    assert "Could not migrate" in caplog.text
