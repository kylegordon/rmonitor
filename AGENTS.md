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

Everything runs in a container built from `tests/Dockerfile` — never pip-install or run
tests on the host.

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
  than skips when a display is missing. A local run is the same 217 tests CI runs.
- **Both interpreters are reachable locally** — the fence's last line switches to the
  3.13 matrix leg, so it is reproducible here rather than CI-only.

`.github/workflows/tests.yml` builds and runs this same image across both matrix legs —
one definition of the test environment. To run the app, see `README.md` §Quick start.

## Project structure and configuration

See `README.md` §Architecture for the layout and §Configuration for the environment-variable
reference. Neither is restated here: both are derivable from the code, and every instance of
documentation drift this repo has had was in restated derivable content. Environment values use a
uniform `os.environ.get("NAME", "default")` idiom in `relay/main.py`, `server/main.py` and
`server/server.py`; `relay/gui.py` merges a `.env` file into the environment *before* importing
`relay.main`, so that import order is load-bearing.

## Code style

No linter or formatter config exists, so style is convention-enforced: read the head of
`server/race_state.py` and match it. PEP 8, 4-space indent, double-quoted strings in tests, type
hints on public functions, reST markup in docstrings, keyword-only parameters after `*`
(`relay/rmonitor_client.py`). `relay/main.py`'s *named* logger is an inconsistency, not the model
to copy.

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

Growth needs a matching drain, or any budget is only a deferred failure. So **every pitfall entry
names the test that guards it**. Prose cannot fail; a rule with no test is a rule an agent breaks
silently while CI stays green, which in a repository nobody hand-writes is the same as no rule at
all. Write that test in the same PR — `tests/test_repo_invariants.py` is where the ones about the
repository's shape live. What an entry then keeps is only what a test cannot tell you: that the
rule exists, and why. The history of how it was discovered belongs in the test's docstring, and is
deleted from here once it lives there.

This file has a hard length budget, because it is loaded in full on every task whatever the task
is. So it holds only what applies to every task. A rule that applies to one directory goes in that
directory's `AGENTS.md` — `server/AGENTS.md`, `tests/AGENTS.md` — beside a one-line `CLAUDE.md`
shim that imports it, which is the only way Claude Code sees a file by that name; CI fails if a
scoped `AGENTS.md` is missing its shim. A detail that belongs to one function goes in that
function's docstring and is not repeated anywhere. **Nothing is ever copied.** Two divergent
copies is the one genuinely undefined configuration, and `.github/instructions/*.instructions.md`
is deliberately unused for the same reason: a second mechanism Copilot reads alongside this one,
with no defined precedence between them, is that trap wearing a different hat.

Moving a section behind an `@` import is not a way to meet the budget. Claude Code resolves those
imports up front, so the check measures the whole eagerly loaded set and an import buys nothing.

## Common pitfalls and workarounds

Every entry here is a fact you need *before* you know which file to open. Where a detail belongs
to one function it lives in that function's docstring and is deliberately not repeated here.

1. **`reg_number` vs `number`**: `reg_number` is the internal registration key (e.g. `"21"`);
   `number` is the displayed car number, possibly with letters (`"12X"`). Always key competitor
   dicts by `reg_number`. `RaceState._competitor` also skips empty incoming values rather than
   blanking a field an earlier `$A`/`$COMP` filled. Guarded by
   `test_competitor_keyed_by_reg_number_not_displayed_number` and
   `test_competitor_name_not_blanked_by_empty_update` in `tests/test_race_state.py`.
2. **The `session_mode` label is the session signal — not `_is_qualifying`.** The label, derived
   by substring match on `run_description`, drives the sort order *and* the interval mode through
   the single `RaceState._sort_mode`. `_is_qualifying` is set by message type and cleared
   permanently by any `$G`, so it measures `False` in every capture here; it survives only as
   `_derive_session_mode`'s first test, catching a pure-`$H` feed that sends no `$B` at all. Read
   `_derive_session_mode`, `_sort_mode`, `_qual_info` and `snapshot` together before touching any
   of them. Guarded by `test_qual_info_during_a_race_does_not_overwrite_race_positions`,
   `test_practice_session_sorts_by_best_lap_and_derives_intervals_from_them`, and the
   `session_mode` and sort-order tests in `tests/test_race_state.py`.
3. **The rMonitor protocol is not fully documented.** `$SP`/`$SR` appear in no spec but are real
   output from some Orbits setups, and `_tokenize` strips quotes *and* whitespace because flag
   strings such as `"Green "` arrive padded. Trust `relay/rmonitor_client.py` over the spec.
   Guarded by `test_heartbeat_flag_trim`, `test_lap_info_sp` and `test_lap_info_sr` in
   `tests/test_parser.py`.
4. **Dependencies belong in a `requirements*.txt`, never in a workflow's `pip install` line.**
   `tests/Dockerfile` installs from all four and is the only place that list exists; why each file
   is copied with its path preserved is commented there. Adding a package to one workflow's inline
   list only ever fixes that workflow, and the next one silently drifts out of sync. Guarded by
   `tests/test_repo_invariants.py`, which also asserts the `xvfb-run` and `conftest.py` rules.

<!-- drift-report:start -->
<!-- drift-report:end -->
