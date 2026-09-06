# tests/AGENTS.md — scoped rules for the test suite

The repository-wide rules in the root `AGENTS.md` apply here in full. These add to them,
and are loaded only when an agent is working in this directory.

## Testing practices

- There is **no `conftest.py`** anywhere and no global `asyncio_mode`, so every async test carries
  an explicit `pytest.mark.asyncio` (`tests/test_server.py`).
- Mocking is `unittest.mock.AsyncMock` applied via `monkeypatch.setattr` (`tests/test_relay_main.py`).
- `tests/test_gui.py` stubs the relay runner and the update poll; `REQUIRE_DISPLAY` turns its
  missing-display case from a skip into a failure.

## The container is the definition of the test environment

`tests/Dockerfile` and `tests/entrypoint.sh` are that definition, used by `./test.sh` locally and
by `.github/workflows/tests.yml` in CI — one image, both matrix legs, no second description of the
environment anywhere. Each non-obvious choice is commented at the line that makes it: the `-slim`
base, `libtk8.6` rather than `python3-tk`, the four requirements files copied with their paths
preserved, why no source is copied, and why Xvfb is started directly and never through `xvfb-run`.
Read those comments before changing either file; every one of them records a failure this repo
actually hit.
