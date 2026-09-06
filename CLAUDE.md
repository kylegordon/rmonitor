# CLAUDE.md — rmonitor

@AGENTS.md

The imported `AGENTS.md` is canonical and applies in full. Everything below is
Claude-specific; it adds to those rules and never overrides them.

## Everything runs in Docker

Never pip-install, run the test suite, or start the app on the host. `AGENTS.md`
§Commands carries the container invocation — `./test.sh`, which forwards any arguments
you append straight to pytest, so use it for scoped runs too.

## RPI phase workflow

Non-trivial work here goes through the RPI phases — research, plan, implement, review
— with a context clear between each. The artifact each phase writes to disk is the
handoff; chat history is disposable.

Those artifacts live in a local `.rpi-tracking/` directory that is **git-ignored and
un-versioned**. They are not repository history, and a reader of the repo cannot open
them. Never cite an artifact path from anything committed — `AGENTS.md`, `README.md`,
a commit message, a PR body. Carry the substance across instead.
