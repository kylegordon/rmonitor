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
