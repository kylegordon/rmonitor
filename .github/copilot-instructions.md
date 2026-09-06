# Copilot Instructions for rmonitor

The canonical agent instructions for this repository are in [`AGENTS.md`](../AGENTS.md)
at the repo root. Copilot coding agent and Copilot code review both read that file
directly, as do Claude Code (via `CLAUDE.md`) and every other agents.md subscriber.

This file is deliberately a pointer and nothing more. Copilot reads both it and
`AGENTS.md` and defines no precedence between them, so a second full copy of the
instructions here would be the one configuration with genuinely undefined behaviour —
and the two copies would drift apart, which is exactly what happened to the 251 lines
this file used to hold.

Rules that apply to one directory live in that directory's own `AGENTS.md` —
`server/AGENTS.md`, `tests/AGENTS.md` — and add to the root file rather than replacing it.

Add and edit rules in `AGENTS.md`. Never here.
