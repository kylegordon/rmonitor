import json
import time

from server.state_store import JsonFileStateStore


def test_round_trip(tmp_path):
    store = JsonFileStateStore(tmp_path / "state.json")
    store.save({"track_name": "Silverstone"})
    assert store.load() == {"track_name": "Silverstone"}


def test_missing_file_returns_empty(tmp_path):
    store = JsonFileStateStore(tmp_path / "does-not-exist.json")
    assert store.load() == {}


def test_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")
    store = JsonFileStateStore(path)
    assert store.load() == {}


def test_fresh_state_within_max_age_is_restored(tmp_path):
    store = JsonFileStateStore(tmp_path / "state.json", max_age_seconds=900)
    store.save({"track_name": "Silverstone"})
    assert store.load() == {"track_name": "Silverstone"}


def test_stale_state_beyond_max_age_is_discarded(tmp_path):
    path = tmp_path / "state.json"
    payload = {"saved_at": time.time() - 3600, "data": {"track_name": "Silverstone"}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = JsonFileStateStore(path, max_age_seconds=900)
    assert store.load() == {}


def test_no_max_age_restores_regardless_of_age(tmp_path):
    path = tmp_path / "state.json"
    payload = {"saved_at": time.time() - 10**6, "data": {"track_name": "Silverstone"}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = JsonFileStateStore(path)
    assert store.load() == {"track_name": "Silverstone"}


def test_repeated_saves_of_unchanged_data_do_not_refresh_staleness(tmp_path):
    """A periodic save loop calls save() unconditionally on every tick, even
    when the underlying race hasn't produced a new message in a while. If
    save() stamped saved_at with wall-clock "now" on every call, that alone
    would keep resetting the age of genuinely stale data back to zero,
    defeating the max-age check entirely. staleness must track the data's
    own last_updated, not the time save() happened to run.
    """
    path = tmp_path / "state.json"
    store = JsonFileStateStore(path, max_age_seconds=900)
    stale_data = {"track_name": "Silverstone", "last_updated": time.time() - 3600}

    for _ in range(3):
        store.save(stale_data)

    assert store.load() == {}
