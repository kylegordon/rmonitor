---
name: rmonitor-testing
description: How rmonitor's tests run and where dependencies go. Use when writing or running tests, touching tests/, test.sh, tests/Dockerfile, a requirements*.txt or a GitHub workflow, or adding a dependency.
paths:
  - "tests/**"
  - test.sh
  - "requirements*.txt"
  - "*/requirements*.txt"
  - ".github/workflows/**"
---

# Running the tests and adding dependencies

The full suite is required before opening any PR. Everything runs in a container built
from `tests/Dockerfile` — never pip-install or run tests on the host. The per-test
conventions (async marks, fixtures, the GUI tests) are in `tests/AGENTS.md`.

## Commands

```sh
./test.sh                                    # whole suite
./test.sh tests/test_gui.py -v               # one file
./test.sh tests/test_gui.py::<test name> -v  # one test
PYTHON_VERSION=3.13 ./test.sh                # the other interpreter CI gates
```

`test.sh` rebuilds the image (warm rebuild ~0.25s), maps your uid/gid on Linux so nothing
comes back root-owned, and runs pytest via `tests/entrypoint.sh`. Three details:

- **Xvfb is started by the entrypoint, never via the `xvfb-run` wrapper**, whose
  wait-for-display poll this repo has twice seen hang indefinitely.
- **`REQUIRE_DISPLAY=1` is baked into the image**, so `tests/test_gui.py` fails rather
  than skips when a display is missing. A local run is the same suite CI runs.
- **Both interpreters are reachable locally** — the fence's last line switches to the
  3.13 matrix leg, so it is reproducible here rather than CI-only.

`.github/workflows/tests.yml` builds and runs this same image across both matrix legs —
one definition of the test environment. To run the app, see `README.md` §Quick start.

## Dependencies belong in a `requirements*.txt`, never in a workflow's `pip install` line

Add a new import's package to the scope-matching file:

- `relay/requirements.txt` — headless relay runtime
- `relay/requirements-build.txt` — GUI and PyInstaller build
- `requirements-dev.txt` — test tooling
- `server/requirements.txt` — server runtime

`tests/Dockerfile` installs from all four and is the only place that list exists; why each
file is copied with its path preserved is commented there. Adding a package to one
workflow's inline list only ever fixes that workflow, and the next one silently drifts out
of sync. Guarded by `tests/test_repo_invariants.py`, which also asserts the `xvfb-run` and
`conftest.py` rules.
