# AGENTS.md — rmonitor

Canonical instructions for every coding agent (Claude Code via `CLAUDE.md`, Copilot directly).
**Edit this file, never a copy.** Every line of code here is agent-authored, so the test suite and
the traps below are the only safety net: turn anything learned the hard way into a guard test.

## Always

- `git fetch origin`, branch from `origin/master`, and land every change through a PR, using
  Conventional Commits with a scope (relay, server, release, ci, deploy, agents). Load the
  `rmonitor-pr` skill before committing.
- Run the full suite with `./test.sh` (extra arguments go to pytest) before opening any PR.
  Load `rmonitor-testing` before writing tests or touching tests, workflows or dependencies.
- Code style: match the head of `server/race_state.py`. That means PEP 8, type hints on public
  functions, reST docstrings, keyword-only parameters after `*`, and double quotes in tests.
- Changing a command, environment variable, workflow, layout, skill or instruction file: load
  `rmonitor-agent-docs` and update the instructions in the same PR.

## Ask first

- New linter, formatter, tooling or runtime; any change to `.github/workflows/release.yml`,
  `.github/workflows/publish.yml` or rMonitor parser public behaviour.

## Never

- Commit or push to `master`, or hand-write a commit subject beginning `chore(master): release `.
- pip-install, run tests or start the app on the host. Everything runs in Docker.
- Put a dependency anywhere but the scope-matching `requirements*.txt` (never a workflow's inline
  `pip install`); use `xvfb-run`; commit secrets (`.env` is gitignored).
- Cite a `.rpi-tracking/` path in anything committed. It is local and git-ignored.

## Where knowledge lives

Layout and configuration: `README.md`. Directory rules: `relay/AGENTS.md`, `server/AGENTS.md`,
`tests/AGENTS.md`. Topic knowledge: skills in `.claude/skills/`. Role subagents in
`.claude/agents/` (`programmer`, `tester`, `docs-keeper`, `reviewer`) only preload those skills.

## Traps: know these before you open a file

Each names the skill that explains it and one test that guards it.

1. `reg_number` is the key; `number` is display text. `rmonitor-feed`;
   `test_competitor_keyed_by_reg_number_not_displayed_number`.
2. `_is_qualifying` overrides the session description until the first `$G`. `rmonitor-feed`;
   `test_qual_info_during_a_race_does_not_overwrite_race_positions`.
3. The protocol is partly undocumented, and `$F` is a fixed six-character field. `rmonitor-feed`;
   `test_captured_flag_fields_are_all_six_characters`.
4. Dependencies live only in `requirements*.txt`. `rmonitor-testing`; `tests/test_repo_invariants.py`.
5. Session boundaries are `$B` edges, never `$I`. `rmonitor-feed`;
   `test_every_opened_session_is_closed_by_a_95_carrying_its_description`.
6. A long-open tab can run an old page against a new server, so prompt and never self-reload.
   `rmonitor-display`; `test_an_outdated_page_prompts_rather_than_reloading_itself`.

<!-- drift-report:start -->
<!-- drift-report:end -->
