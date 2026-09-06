# CLAUDE.md — server/

@AGENTS.md

Claude Code does not read a file called `AGENTS.md`; this one-line import is the only way
the scoped rules beside it reach a Claude session working in this directory. Copilot finds
`server/AGENTS.md` on its own. Keep this file a shim — rules go in `AGENTS.md`, never here,
or the two agents start obeying different instructions in the same directory.
