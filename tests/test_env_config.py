"""Tests for relay/env_config.py's `.env` reader/writer and path resolution."""

from relay import env_config


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
