---
name: rmonitor-pr
description: The mandatory git and pull-request workflow for rmonitor. Use before branching, committing, pushing or opening a pull request in this repository.
---

# Branch, commit and open a PR

**All changes must go through a pull request. Never commit directly to `master`.** This
applies to every change, no matter how small. The `master` ruleset also rejects direct
pushes server-side, so the rule is enforced, not just written down.

1. **Fetch first:** `git fetch origin` — always, before branching or pushing.
2. **Branch:** `git checkout -b <type>/<slug> origin/master`. Types in use: feat, fix, docs,
   ci, chore; an issue-linked slug is fine (`issue-69-centre-empty-state`).
3. Commit on the branch, using **Conventional Commits with a scope** —
   `fix(server): centre empty-state message`. Scopes: relay, server, release, ci, deploy,
   agents.
4. **Check PR status before pushing:** `gh pr list --head <branch>` — if a PR for this
   branch already exists and is merged or closed, branch again rather than reusing it.
5. **Push, then open the PR:** `git push -u origin <branch>` and
   `gh pr create --base master --fill`.
6. Do **not** merge or push to `master` directly under any circumstances.

Never hand-write a commit whose subject begins `chore(master): release ` —
`.github/workflows/release.yml` gates on that literal string.

Run the full suite with `./test.sh` before opening the PR (the `rmonitor-testing` skill).
