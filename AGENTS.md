# AGENTS.md — rmonitor

Canonical instructions for every coding agent here. Claude Code reads them through the `@AGENTS.md`
import in `CLAUDE.md`; `.github/copilot-instructions.md` points here. **Edit this file, never a
copy** — two divergent copies is the one genuinely undefined configuration.

## Every line of code here is agent-authored

Humans task and review; they do not write the code. No human will catch a subtle bug by having
written the surrounding code, so **the test suite and the pitfalls list below are the only safety
net.** Hence the strictness: run the tests, obey the boundaries, and add what you learn the hard
way to the pitfalls list in the same PR.

## Git workflow — MANDATORY

**All changes must go through a pull request. Never commit directly to `master`.**

1. **Fetch first:** `git fetch origin` — always, before branching or pushing.
2. **Branch:** `git checkout -b <type>/<slug> origin/master`. Types in use: feat, fix, docs, ci,
   chore; an issue-linked slug is fine (`issue-69-centre-empty-state`).
3. Commit on the branch, using **Conventional Commits with a scope** —
   `fix(server): centre empty-state message`. Scopes: relay, server, release, ci, deploy, agents.
4. **Check PR status before pushing:** `gh pr list --head <branch>` — if a PR for this branch
   already exists and is merged or closed, branch again rather than reusing it.
5. **Push, then open the PR:** `git push -u origin <branch>` and
   `gh pr create --base master --fill`.
6. Do **not** merge or push to `master` directly under any circumstances.

This applies to every change, no matter how small. Never hand-write a commit whose subject begins
`chore(master): release ` — `.github/workflows/release.yml` gates on that literal string.

## Commands — the full test suite, required before opening any PR

Everything runs in a throwaway Docker container — never pip-install or run tests on the host.

```sh
docker run --rm -v "$PWD":/app -w /app -e REQUIRE_DISPLAY=1 python:3.12 sh -c '
set -e
apt-get update -qq
apt-get install -y -qq xvfb
pip install -q -r requirements-dev.txt -r relay/requirements.txt -r relay/requirements-build.txt -r server/requirements.txt
Xvfb :99 -screen 0 1280x1024x24 &
for _ in $(seq 1 50); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.2; done
[ -S /tmp/.X11-unix/X99 ] || { echo "Xvfb failed to start on :99" >&2; exit 1; }
export DISPLAY=:99
python -m pytest tests/ -v
'
```

Swap the last line to scope a run, e.g. `python -m pytest tests/test_gui.py -v`. Three details:

- **Xvfb is started directly, never via the `xvfb-run` wrapper**, whose wait-for-display poll this
  repo has twice seen hang indefinitely. CI's "Start Xvfb" step does the same, so the two match.
- **`REQUIRE_DISPLAY=1` is mandatory.** Without it `tests/test_gui.py` *skips* rather than fails
  when there is no display, and you get a green local run against a red CI run.
- **The container is Python 3.12 only**, while CI gates 3.12 *and* 3.13, and provisions Tk
  differently. A local pass is necessary, not sufficient; the environments are not equivalent.

`.github/workflows/tests.yml` does the same four-file install on the runner host and runs
`python -m pytest tests/ -v` with `REQUIRE_DISPLAY` set, across a 3.12/3.13 matrix. To run the app,
see `README.md` §Quick start.

## Project structure and configuration

See `README.md` §Architecture for the layout and §Configuration for the environment-variable
reference. Neither is restated here: both are derivable from the code, and every instance of
documentation drift this repo has had was in restated derivable content. Environment values use a
uniform `os.environ.get("NAME", "default")` idiom in `relay/main.py`, `server/main.py` and
`server/server.py`; `relay/gui.py` merges a `.env` file into the environment *before* importing
`relay.main`, so that import order is load-bearing.

## Code style

No linter or formatter config exists, so style is convention-enforced. Follow `server/race_state.py`:

```python
"""In-memory race state built from rMonitor messages.

State is populated by the parsed dicts from ``rmonitor_client.parse_line``.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _lap_time_seconds(t: str) -> float | None:
    """Convert ``HH:MM:SS.mmm`` to total seconds, or *None*."""
```

PEP 8, 4-space indent, double-quoted strings in tests, type hints on public functions, reST markup
in docstrings, keyword-only parameters after `*` (`relay/rmonitor_client.py`). `relay/main.py`'s
*named* logger is an inconsistency, not the model to copy.

## Testing practices

- There is **no `conftest.py`** anywhere and no global `asyncio_mode`, so every async test carries
  an explicit `pytest.mark.asyncio` (`tests/test_server.py`).
- Mocking is `unittest.mock.AsyncMock` applied via `monkeypatch.setattr` (`tests/test_relay_main.py`).
- `tests/test_gui.py` stubs the relay runner and the update poll; `REQUIRE_DISPLAY` turns its
  missing-display case from a skip into a failure.

## Dependencies

Add a new import's package to the scope-matching `requirements*.txt` — `relay/requirements.txt`
(headless runtime), `relay/requirements-build.txt` (GUI/PyInstaller), `requirements-dev.txt` (test
tooling), `server/requirements.txt` — **never** a workflow's inline `pip install`; see pitfall 11.

## Boundaries

- **Always** — run the full suite in Docker before opening a PR; update this file in the same PR
  as the change that dates it.
- **Ask first** — adding a linter, formatter, or any new tooling or runtime; changing
  `.github/workflows/release.yml` or `.github/workflows/publish.yml`; changing rMonitor parser
  public behaviour.
- **Never** — commit to `master`; commit secrets (`.env` is gitignored, `.env.example` holds
  placeholders only); add a dependency to a workflow's inline `pip install`; use `xvfb-run` here.

## Keeping this file current

A PR that changes a command, an environment variable, a workflow or the project layout **updates
this file in the same PR**, and an agent that hits a non-obvious failure **adds it to the pitfalls
list in the same PR** — the only way this memory grows. A CI check validates the mechanical half
(referenced paths and environment variables are real); the prose half is on you.

## Common pitfalls and workarounds

1. **`reg_number` vs `number`**: `reg_number` is the internal registration key (e.g. `"21"`);
   `number` is the displayed car number, possibly with letters (`"12X"`). Always key competitor
   dicts by `reg_number`.
2. **Empty-string field updates**: `RaceState._competitor` skips updating a field when the incoming
   value is empty, to avoid blanking data from an earlier `$A`/`$COMP` message.
3. **Qualifying vs race mode**: `_is_qualifying` is set on `$H` and cleared on `$G`. Snapshot sort
   order follows: `best_lap_time` ascending for qualifying, `position` numeric for race,
   `total_time` ascending under a purple flag.
4. **Speed calculation**: `last_lap_speed_mph` is computed only when both `track_length_miles`
   (from `$E TRACKLENGTH`) and a positive lap time exist; zero or missing yields `None`.
5. **`$SP`/`$SR` are undocumented**: real messages from some Orbits setups, carrying per-lap
   position and time; they map to `lap_info`.
6. **Protocol token quirks**: `_tokenize()` strips double-quotes and whitespace from every token;
   flag strings in `$F` can have trailing spaces (`"Green "`), removed by `strip()`.
7. **No linter/formatter config**: follow existing style (see Code style above).
8. **aiohttp: never reassign `AppKey` values after app startup.** `app[some_key] = new_value` in a
   request handler or background task triggers `DeprecationWarning: Changing state of started or
   joined application is deprecated`. Store a mutable container under the key at creation and
   mutate it:
   ```python
   # At app creation (fine):
   app[my_key] = {"value": None, "flag": False}
   # In a handler (fine — mutating the dict, not the key):
   app[my_key]["value"] = time.monotonic()
   # BAD — reassigning the key after startup:
   app[my_key] = time.monotonic()
   ```
9. **Server-side stateful features: initialise timestamps at startup, not lazily.** If a watchdog
   or timeout compares `time.monotonic() - last_seen`, initialise `last_seen` to `time.monotonic()`
   in the app startup hook, not to `None` — an `if last is not None` guard never fires on a fresh
   server that has never received data. Similarly, when a new WebSocket client connects,
   `handle_ws` must immediately reflect the *current* server state: if the feed is already known
   lost or timed out, send `no_feed` right after the initial `full` message, so the client does not
   show stale persisted data until the next watchdog poll.
10. **Session mode detection has several non-obvious behaviours** — understand all of these before
    touching `_derive_session_mode`, `_is_qualifying` or `_seen_race_info`:
    - *Two-track detection — two independent signals that must agree.* `_is_qualifying` (bool) is
      set **by message type**: True when a `$H` (qual_info) arrives while `_seen_race_info` is still
      False, cleared on any `$G` (race_info); it controls sort order directly in `snapshot()`. The
      `session_mode` label is derived by `_derive_session_mode()` via **substring matching** on
      `run_description` (from `$B`) for `"practice"`, `"prac"`, `"familiarisation"`, `"qual"`.
      `_is_qualifying=True` takes priority — it returns `"Qualifying"` without reading it.
    - *Warm-up is now handled.* `"warm"` is in the practice keyword list, covering "Warm up",
      "Warm-up", "Warmup"; the full list is `"practice"`, `"prac"`, `"warm"`, `"familiarisation"`.
      A description matching neither those nor `"qual"` falls through to `"Race"` if
      `_seen_race_info` or `run_description` is set — always extend it for a new session type.
    - *`$H` during a race.* Some Orbits setups send `$H` (qual_info) during a race for best-lap
      tracking. The `if not self._seen_race_info` guard prevents `_is_qualifying` being set and `$H`
      positions overwriting `$G` race positions; best-lap fields (`best_lap_time`, `best_lap`) are
      always updated regardless.
    - *Purple flag overrides sort regardless of mode.* Under a purple flag `snapshot()` uses
      `_sort_key_purple` (total_time ascending) whatever `_is_qualifying` says — intended for
      formation/pace laps at race end, where cars are on track in order of total time.
    - *String matching is spelling-sensitive.* `_derive_session_mode()` handles `"familiarisation"`
      (British English) but silently returns `"Race"` for anything unrecognised ("Free Practice",
      "Shakedown").
11. **Dependencies belong in a `requirements*.txt`, never hand-listed in a workflow's
    `pip install` line.** `.github/workflows/tests.yml` used to run
    `pip install pytest pytest-asyncio aiohttp` directly, so when `relay/gui.py` and
    `relay/env_config.py` started importing `platformdirs` (declared only in
    `relay/requirements-build.txt`), the test job's install step did not pick it up and
    `tests/test_gui.py` and `tests/test_env_config.py` failed collection with `ModuleNotFoundError`
    — while the release and publish workflows, which already installed with `-r` from those files,
    were unaffected. It now installs from the same four files the build/release workflows and local
    dev use. Adding a package to one workflow's inline list only fixes that workflow; the next
    silently drifts out of sync.

<!-- drift-report:start -->
<!-- drift-report:end -->
