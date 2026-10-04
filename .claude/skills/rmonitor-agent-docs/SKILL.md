---
name: rmonitor-agent-docs
description: How rmonitor's agent instructions are laid out, budgeted and kept current. Use when editing any AGENTS.md, CLAUDE.md, .github/copilot-instructions.md, a skill under .claude/skills, a role agent under .claude/agents or check_agents_md.py, or when changing a command, environment variable, workflow or layout the instructions name.
paths:
  - AGENTS.md
  - CLAUDE.md
  - "**/AGENTS.md"
  - "**/CLAUDE.md"
  - ".claude/skills/**"
  - ".claude/agents/**"
  - .github/copilot-instructions.md
  - .github/scripts/check_agents_md.py
---

# Keeping the agent instructions current

Every line of code here is agent-authored, so the instruction files and the test suite are
the repository's whole encoding of its standards. This skill says where a rule goes, how
it stays honest, and what budgets hold it.

## Keep them current in the same PR

A PR that changes a command, an environment variable, a workflow or the project layout
**updates the instructions in the same PR**, and an agent that hits a non-obvious failure
**adds it as a trap in the same PR** — the only way this memory grows.
`.github/scripts/check_agents_md.py` checks the mechanical half (referenced paths and
environment variables are real, frontmatter is well-formed, budgets hold); it runs inside
`./test.sh` as `test_the_instruction_files_pass_their_own_check`. The prose half is on you.

## Every trap names the test that guards it

Growth needs a matching drain, or any budget is only a deferred failure. Prose cannot
fail; a rule with no test is a rule an agent breaks silently while CI stays green, which in
a repository nobody hand-writes is the same as no rule at all. Write that test in the same
PR — `tests/test_repo_invariants.py` is where the ones about the repository's shape live.
What an entry then keeps is only what a test cannot tell you: that the rule exists, and
why. The history of how it was discovered belongs in the test's docstring, and is deleted
from the instructions once it lives there.

## Where a rule goes

| Applies to | Lives in | Budget |
|---|---|---|
| every task | root `AGENTS.md` (eager, with `CLAUDE.md`) | under 60 lines |
| one directory | that directory's `AGENTS.md` + a one-line `CLAUDE.md` shim | under 80 |
| one topic | a skill, `.claude/skills/<name>/SKILL.md` | under 150; description ≤1,536 chars |
| one role | a thin agent, `.claude/agents/<name>.md` | under 40 |
| one function | that function's docstring | — |

The budgets are the `MAX_*` constants in `.github/scripts/check_agents_md.py`; change
both together. That script finds environment variables only through the
`os.environ.get("NAME", "default")` idiom, so a setting read any other way and named in
an instruction file is reported as drift.

Every project skill is named `rmonitor-*`, or is `rpi-artifacts`, so the checker can fail
on a cited skill that no longer exists; the role agents root `AGENTS.md` names are also
listed in the checker, and adding or removing one changes both.

The scoped files are `relay/AGENTS.md`, `server/AGENTS.md` and `tests/AGENTS.md`. The shim
is the only way Claude Code sees a file by that name, and CI fails if one is missing.
Skills are read by Claude Code and by Copilot cloud agent and code review alike, so
knowledge goes in a skill. A role agent only names a role, a finish line and the skills it
preloads with `skills:`: knowledge in its body would be a Claude-only copy Copilot never
reads. A skill that outgrows its budget moves detail into a supporting file beside its
SKILL.md or into the docstring of the code it describes.

## Nothing is ever copied

Two divergent copies is the one genuinely undefined configuration. `.github/copilot-instructions.md`
stays a pointer. `.github/instructions/*.instructions.md` and GitHub's custom-agent files
are deliberately unused for the same reason: a second mechanism read alongside this one, with
no defined precedence between them, is that trap wearing a different hat.

Moving a section behind an `@` import is not a way to meet the budget. Claude Code
resolves those imports up front, so the check measures the whole eagerly loaded set and an
import buys nothing.
