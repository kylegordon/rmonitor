---
name: rpi-artifacts
description: Where RPI phase artifacts live in rmonitor and why they are never cited. Use when running an RPI phase (research, plan, implement, review) here, or when writing a commit message, PR body or doc after one.
---

# RPI artifacts

Non-trivial work here goes through the RPI phases — research, plan, implement, review —
with a context clear between each. The artifact each phase writes to disk is the handoff;
chat history is disposable.

Those artifacts live in a local `.rpi-tracking/` directory that is **git-ignored and
un-versioned**. They are not repository history, and a reader of the repo cannot open
them. Never cite an artifact path from anything committed — `AGENTS.md`, `README.md`, a
skill, a commit message, a PR body. Carry the substance across instead.
