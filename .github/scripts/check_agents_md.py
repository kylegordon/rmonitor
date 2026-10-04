#!/usr/bin/env python3
"""Validate that the agent instruction files still describe this repository.

Every line of code here is written by an agent, so ``AGENTS.md`` is the repository's
entire encoding of its engineering standards.  Before this check existed, the single
instruction file had drifted from the code in four measurable ways.  Three of those
were mechanically detectable, and this script detects them: it asserts that every
repo-relative path and every environment variable named in the instruction files is
real, that the ``@AGENTS.md`` import the no-duplication design rests on is intact,
that the Copilot pointer has not regrown into a second full copy, that every scoped
``AGENTS.md`` has the ``CLAUDE.md`` shim that makes Claude Code see it, that the
instruction set an agent carries on every task stays inside its length budget, and
that the on-demand skills in ``.claude/skills/`` and the thin role agents in
``.claude/agents/`` are well-formed and inside theirs.

Stdlib only, deliberately.  Adding a dependency for a documentation check would break
the very rule this file exists to protect (the dependency rule in ``AGENTS.md``'s
Never list).

Run with no arguments to check and exit non-zero on drift.  Run with ``--annotate``
to write the findings into the drift-report region of ``AGENTS.md`` instead, which is
what the scheduled audit does before opening a PR.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

#: Instruction files at the repo root.  Scoped ones are discovered, not listed; see
#: ``instruction_files``.
ROOT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md")

#: Budget for the *eagerly loaded* set — ``CLAUDE.md`` plus everything it reaches
#: through ``@`` imports, which Claude Code resolves up front.  Budgeting the root
#: ``AGENTS.md`` alone was gameable: moving a section behind an ``@`` import satisfies a
#: per-file limit while costing an agent exactly as much context as before.  Cut from
#: 200 to 60 when the domain and process knowledge moved into skills, which load only
#: when a task needs them: every eager line is paid again by every spawned subagent,
#: whatever its task, so the eager set holds only what applies to all of them.
MAX_EAGER_LINES = 60

#: Budget for one scoped ``AGENTS.md``, loaded only when an agent works in that
#: directory.  Smaller than the eager budget on purpose: a scoped file needing more
#: than this is describing a subsystem, and the subsystem's own docstrings are where
#: that belongs.
MAX_SCOPED_LINES = 80

#: Budget for one skill's ``SKILL.md``.  The body loads whole when the skill fires, so
#: a longer one belongs in a supporting file beside it, read only when needed, or in
#: the docstring of the code it describes.
MAX_SKILL_LINES = 150

#: Budget for one role agent.  Agents are deliberately thin — a role, a finish line
#: and a ``skills:`` preload — because knowledge in an agent body is a Claude-only copy
#: that Copilot never reads.
MAX_AGENT_LINES = 40

#: Claude Code truncates a skill's ``description`` plus ``when_to_use`` in the skill
#: listing at this many characters, so anything past it never reaches the model that
#: decides whether to load the skill.
MAX_SKILL_DESCRIPTION_CHARS = 1536

#: Where project skills and role agents live.
SKILLS_DIR = ".claude/skills"
AGENTS_DIR = ".claude/agents"

#: The role agents root ``AGENTS.md`` promises.  Discovery alone cannot notice one
#: being deleted — there is no file left to check — so they are listed here, and
#: ``check_named_skills_and_agents`` asserts both that each exists and that
#: ``AGENTS.md`` still names it, so neither list can drift from the other silently.
ROLE_AGENTS = ("programmer", "tester", "docs-keeper", "reviewer")

#: A backtick span of this form in an instruction file names a project skill, and
#: must therefore have a ``SKILL.md``: every project skill is ``rmonitor-*`` or the one
#: ``rpi-artifacts``, which is what lets a deleted or renamed skill be caught by its
#: citations.  Not ``rpi-*`` as a whole: the other RPI phase skills are user-level, not
#: this repository's, and naming one in an instruction file is legitimate.
_SKILL_NAME_RE = re.compile(r"^(?:rmonitor-[a-z0-9]+(?:-[a-z0-9]+)*|rpi-artifacts)$")

#: A YAML block-scalar header: ``|`` or ``>``, an optional indent digit and chomping
#: indicator in either order, and an optional trailing comment.
_BLOCK_SCALAR_RE = re.compile(r"[|>](?:[1-9][-+]?|[-+][1-9]?)?\s*(?:#.*)?")

#: Maximum depth Claude Code follows ``@`` imports.
MAX_IMPORT_DEPTH = 5

#: The Copilot pointer must stay a pointer.  Copilot reads it *and* ``AGENTS.md`` with
#: no defined precedence, so a second full copy is genuinely undefined behaviour.
MAX_COPILOT_LINES = 20

#: Paths that legitimately do not exist in a checkout.
ALLOWED_MISSING_PATHS = frozenset(
    {
        # The RPI phase artifacts are deliberately local and git-ignored, so a CI
        # checkout never has them.  AGENTS.md and the rpi-artifacts skill name the
        # directory precisely to say that committed docs must not cite paths inside it.
        ".rpi-tracking/",
        # tests/AGENTS.md names conftest.py in order to say there isn't one, which is why
        # every async test has to carry its own pytest.mark.asyncio.  Its absence is
        # the documented fact; asserting its presence would invert the check.
        "conftest.py",
    }
)

#: Environment variables the code really uses but that the ``os.environ.get`` scan
#: below cannot see.
ALLOWED_ENV_NAMES = frozenset(
    {
        # A key written into and read back from the GUI's .env file via
        # relay/env_config.py, never through os.environ.get.
        "GUI_WINDOW_GEOMETRY",
    }
)

#: Extensions that make a bare, slash-free token a path rather than prose.
PATH_SUFFIXES = (
    ".py",
    ".md",
    ".yml",
    ".yaml",
    ".txt",
    ".json",
    ".toml",
    ".cfg",
    ".ini",
    ".sh",
    ".spec",
)

#: Characters that mean a token is shell, code or a placeholder, not a path.
_NOT_A_PATH = set("$()[]{}<>\"'*?|`,=!@%^&")

_FENCE_RE = re.compile(r"^\s*```")
_BACKTICK_RE = re.compile(r"`+([^`]+)`+")
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_ENV_ASSIGN_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,})=")
_ENV_READ_RE = re.compile(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""")

DRIFT_START = "<!-- drift-report:start -->"
DRIFT_END = "<!-- drift-report:end -->"

_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "build", "dist"}

#: Repo-relative prefixes never scanned for instruction files.  The harness leaves
#: whole copies of the repository under ``.claude/worktrees/``; they are other
#: checkouts' files, not this one's.  ``.claude`` itself is not skipped, because the
#: skills and role agents live under it.
_SKIP_PREFIXES = (".claude/worktrees/",)


def repo_root() -> Path:
    """Return the repository root, derived from this script's own location."""
    return Path(__file__).resolve().parents[2]


def split_fences(text: str) -> tuple[str, list[str]]:
    """Split *text* into its prose and its fenced code blocks.

    The two are checked differently: paths are looked for in both, because the
    documented four-file ``pip install`` line lives inside a fence, while environment
    names are looked for in prose only, since a shell snippet is full of variables
    like ``DISPLAY`` that belong to the shell rather than to this project.
    """
    prose: list[str] = []
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
            continue
        if current is None:
            prose.append(line)
        else:
            current.append(line)
    if current is not None:  # unterminated fence; treat what we have as a block
        blocks.append("\n".join(current))
    return "\n".join(prose), blocks


def backtick_spans(text: str) -> list[str]:
    """Return the contents of every backtick span in *text*, unsplit."""
    return _BACKTICK_RE.findall(text)


def _candidate_tokens(text: str) -> list[str]:
    """Yield whitespace-separated tokens from every backtick span in *text*."""
    tokens: list[str] = []
    for span in _BACKTICK_RE.findall(text):
        tokens.extend(span.split())
    return tokens


def looks_like_path(token: str, root: Path) -> str | None:
    """Return *token* normalised as a repo-relative path, or ``None`` if it is not one.

    Conservative by design: a false positive here fails CI on prose, which would teach
    agents to distrust the check.  A slash alone is not enough to make a token a path —
    ``origin/master`` is a git ref — so a slashed token also has to start with a segment
    that really exists in the repo, unless it carries a source-file extension.
    """
    token = token.strip().rstrip(".,;:")
    token = _LINE_SUFFIX_RE.sub("", token)
    if not token or token in {".", ".."}:
        return None
    if _NOT_A_PATH & set(token):
        return None
    if "://" in token or token.startswith(("http", "/", "~", "-")):
        return None
    if token.endswith(PATH_SUFFIXES):
        return token
    if "/" in token and (root / token.split("/", 1)[0]).exists():
        return token
    return None


def instruction_files(root: Path) -> list[str]:
    """Return every instruction file in the repo, root and scoped alike.

    Scoped files, skills and role agents are discovered rather than listed, so a new
    ``server/AGENTS.md`` or skill is checked for stale paths from the moment it is
    committed and nobody has to remember to register it here.
    """
    names = list(ROOT_INSTRUCTION_FILES)
    for path in sorted(root.rglob("AGENTS.md")) + sorted(root.rglob("CLAUDE.md")):
        rel = path.relative_to(root)
        if len(rel.parts) == 1 or set(rel.parts) & _SKIP_DIRS:
            continue
        if rel.as_posix().startswith(_SKIP_PREFIXES):
            continue
        names.append(rel.as_posix())
    names.extend(skill_files(root))
    names.extend(role_agent_files(root))
    return names


def skill_files(root: Path) -> list[str]:
    """Return every ``.claude/skills/*/SKILL.md``, repo-relative and sorted."""
    return [
        path.relative_to(root).as_posix()
        for path in sorted((root / SKILLS_DIR).glob("*/SKILL.md"))
    ]


def role_agent_files(root: Path) -> list[str]:
    """Return every ``.claude/agents/*.md``, repo-relative and sorted."""
    return [
        path.relative_to(root).as_posix()
        for path in sorted((root / AGENTS_DIR).glob("*.md"))
    ]


def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str] | None:
    """Return the frontmatter mapping of *text* and the body after it.

    Returns ``None`` when *text* does not open with a ``---`` line closed by another.
    This is a deliberate subset of YAML — ``key: value`` scalars with matching
    surrounding quotes stripped, a ``key:`` line followed by ``  - item`` lines, an
    inline ``key: [a, b]`` list, and ``|`` / ``>`` block scalars, folded to one string;
    anything else stays a raw string — because the check is stdlib only and the files
    it reads are written to this subset.  Block scalars are read rather than left raw
    because a long folded ``description`` is exactly what the truncation check is for.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None
    fields: dict[str, str | list[str]] = {}
    current: str | None = None
    block: tuple[str, str, list[str]] | None = None  # (key, style, lines)

    def close_block() -> None:
        if block is not None:
            key, style, parts = block
            fields[key] = ("\n" if style == "|" else " ").join(parts).strip()

    for line in lines[1:end]:
        stripped = line.strip()
        if block is not None and (not stripped or line[:1].isspace()):
            if stripped:
                block[2].append(stripped)
            continue
        close_block()
        block = None
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current is not None:
            items = fields[current]
            if isinstance(items, list):
                items.append(_unquote(stripped[2:].strip()))
            continue
        key, sep, value = line.partition(":")
        if not sep or line[:1].isspace():
            continue
        key, value = key.strip(), value.strip()
        current = None
        if not value:
            fields[key] = []
            current = key
        elif _BLOCK_SCALAR_RE.fullmatch(value):
            block = (key, value[0], [])
        elif value.startswith("[") and value.endswith("]"):
            fields[key] = [_unquote(v.strip()) for v in value[1:-1].split(",") if v.strip()]
        else:
            fields[key] = _unquote(value)
    close_block()
    return fields, "\n".join(lines[end + 1 :])


def _unquote(value: str) -> str:
    """Strip one pair of matching surrounding quotes from *value*."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def strip_drift_region(text: str) -> str:
    """Return *text* with the drift-report region removed.

    ``--annotate`` writes that region and it can run to dozens of lines.  Counting it
    would let a drift report push ``AGENTS.md`` past its budget and manufacture a
    second finding on top of the real one — the audit reporting a fault it created.
    """
    if DRIFT_START not in text or DRIFT_END not in text:
        return text
    start = text.index(DRIFT_START)
    end = text.index(DRIFT_END) + len(DRIFT_END)
    return text[:start] + text[end:]


def eager_set(root: Path) -> list[tuple[str, int]]:
    """Return ``(path, line count)`` for every file Claude Code loads up front.

    That is ``CLAUDE.md`` and, recursively, every file it pulls in with a line-start
    ``@`` import.  Fences are stripped first for the reason given in
    ``check_claude_import``: an ``@`` inside a fence is displayed, not imported.
    """
    # Both sides resolved: an import is written relative to the importing file, and on
    # a symlinked root (a macOS /tmp, say) an unresolved comparison would place every
    # imported file outside the repo and silently count nothing.
    base = root.resolve()
    seen: set[Path] = set()
    found: list[tuple[str, int]] = []

    def walk(path: Path, depth: int) -> None:
        if depth > MAX_IMPORT_DEPTH or not path.is_file():
            return
        resolved = path.resolve()
        if resolved in seen or not resolved.is_relative_to(base):
            return
        seen.add(resolved)
        text = strip_drift_region(path.read_text(encoding="utf-8"))
        found.append((resolved.relative_to(base).as_posix(), len(text.splitlines())))
        prose, _ = split_fences(text)
        for line in prose.splitlines():
            if line.startswith("@"):
                walk(path.parent / line[1:].strip(), depth + 1)

    walk(root / "CLAUDE.md", 0)
    return found


def scoped_agents_files(root: Path) -> list[str]:
    """Return every non-root ``AGENTS.md``, repo-relative."""
    return [
        name
        for name in instruction_files(root)
        if name.endswith("AGENTS.md") and "/" in name
    ]


def check_paths(root: Path) -> list[str]:
    """Assert every repo-relative path named in the instruction files exists."""
    findings: list[str] = []
    for name in instruction_files(root):
        path = root / name
        if not path.is_file():
            findings.append(f"{name}: missing — the instruction file itself is gone")
            continue
        prose, blocks = split_fences(path.read_text(encoding="utf-8"))
        tokens = _candidate_tokens(prose)
        for block in blocks:
            tokens.extend(block.split())
            tokens.extend(_candidate_tokens(block))
        seen: set[str] = set()
        for token in tokens:
            candidate = looks_like_path(token, root)
            if candidate is None or candidate in seen:
                continue
            seen.add(candidate)
            if candidate in ALLOWED_MISSING_PATHS:
                continue
            if not (root / candidate).exists():
                findings.append(f"{name}: references `{candidate}`, which does not exist")
    return findings


def code_env_names(root: Path) -> set[str]:
    """Collect every environment variable the Python sources actually read."""
    names: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            source = Path(dirpath, filename).read_text(encoding="utf-8", errors="replace")
            names.update(_ENV_READ_RE.findall(source))
    return names


def check_env_names(root: Path) -> list[str]:
    """Assert every environment variable named in the instruction files is real."""
    known = code_env_names(root) | set(ALLOWED_ENV_NAMES)
    findings: list[str] = []
    for name in instruction_files(root):
        path = root / name
        if not path.is_file():
            continue  # already reported by check_paths
        prose, _ = split_fences(path.read_text(encoding="utf-8"))
        seen: set[str] = set()
        # Whole spans only, never their words: `$E TRACKLENGTH` names a protocol field
        # inside a message, not an environment variable.
        for span in backtick_spans(prose):
            match = _ENV_ASSIGN_RE.match(span)
            candidate = match.group(1) if match else span
            if not _ENV_NAME_RE.match(candidate) or candidate in seen:
                continue
            seen.add(candidate)
            if candidate not in known:
                findings.append(
                    f"{name}: names environment variable `{candidate}`, "
                    "which no source file reads"
                )
    return findings


def check_claude_import(root: Path) -> list[str]:
    """Assert the one import line that keeps Claude and Copilot on the same source.

    Fenced blocks are stripped first, because Claude Code's own import parsing skips
    Markdown code spans and fenced blocks: an ``@AGENTS.md`` inside a fence is shown,
    not imported, and would leave Claude reading nothing.
    """
    findings: list[str] = []
    claude = root / "CLAUDE.md"
    if not claude.is_file():
        return ["CLAUDE.md: missing"]
    prose, _ = split_fences(claude.read_text(encoding="utf-8"))
    lines = prose.splitlines()
    if "@AGENTS.md" not in lines:
        findings.append(
            "CLAUDE.md: no line-start `@AGENTS.md` import — Claude Code would stop "
            "reading AGENTS.md entirely"
        )
    if not (root / "AGENTS.md").is_file():
        findings.append("AGENTS.md: missing — the canonical instruction file is gone")
    return findings


def check_copilot_pointer(root: Path) -> list[str]:
    """Assert the Copilot instructions have not regrown into a second full copy."""
    path = root / ".github/copilot-instructions.md"
    if not path.is_file():
        return [".github/copilot-instructions.md: missing"]
    text = path.read_text(encoding="utf-8")
    findings: list[str] = []
    if "AGENTS.md" not in text:
        findings.append(
            ".github/copilot-instructions.md: does not name AGENTS.md, so it no longer "
            "points anywhere"
        )
    count = len(text.splitlines())
    if count > MAX_COPILOT_LINES:
        findings.append(
            f".github/copilot-instructions.md: {count} lines, over the "
            f"{MAX_COPILOT_LINES}-line pointer budget — it is becoming a second copy"
        )
    return findings


def check_eager_budget(root: Path) -> list[str]:
    """Assert the eagerly loaded instruction set stays inside its budget."""
    files = eager_set(root)
    if not files:
        return []  # already reported by check_claude_import
    total = sum(count for _, count in files)
    if total < MAX_EAGER_LINES:
        return []
    breakdown = ", ".join(f"{name} {count}" for name, count in files)
    return [
        f"eagerly loaded instruction set: {total} lines, at or over the "
        f"{MAX_EAGER_LINES}-line budget ({breakdown}). Move a directory-specific rule "
        "to that directory's AGENTS.md, push a detail down into the docstring of the "
        "function it describes, or retire a pitfall the test suite now covers. Moving "
        "lines behind an `@` import does not help — the import is eager and is counted "
        "here."
    ]


def check_scoped_budgets(root: Path) -> list[str]:
    """Assert each scoped ``AGENTS.md`` stays inside the smaller scoped budget."""
    findings: list[str] = []
    for name in scoped_agents_files(root):
        count = len((root / name).read_text(encoding="utf-8").splitlines())
        if count >= MAX_SCOPED_LINES:
            findings.append(
                f"{name}: {count} lines, at or over the {MAX_SCOPED_LINES}-line scoped "
                "budget — that much detail belongs in the subsystem's own docstrings"
            )
    return findings


def check_nested_shims(root: Path) -> list[str]:
    """Assert every scoped ``AGENTS.md`` has its ``CLAUDE.md`` shim beside it.

    Copilot finds a nested ``AGENTS.md`` by itself.  Claude Code never reads a file by
    that name and reaches it only through an import, so without the one-line sibling
    shim the two agents silently obey different rules in the same directory — the exact
    failure this repository's no-duplication design exists to prevent.
    """
    findings: list[str] = []
    for name in scoped_agents_files(root):
        rel = name.replace("AGENTS.md", "CLAUDE.md")
        shim = root / rel
        if not shim.is_file():
            findings.append(
                f"{rel}: missing — {name} exists, so Claude Code needs the one-line "
                "`@AGENTS.md` shim beside it or it never sees those rules"
            )
            continue
        prose, _ = split_fences(shim.read_text(encoding="utf-8"))
        if "@AGENTS.md" not in prose.splitlines():
            findings.append(
                f"{rel}: no line-start `@AGENTS.md` import, so {name} reaches Copilot "
                "but not Claude Code"
            )
    return findings


def _as_text(value: str | list[str] | None) -> str:
    """Return a frontmatter value as one string; a list is joined, ``None`` is empty."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(value)
    return value


def check_skills(root: Path) -> list[str]:
    """Assert every project skill is well-formed and inside its budgets.

    A skill fires on its ``description`` alone, so a missing or truncated one is a
    skill that never loads, and a ``name`` that differs from its directory is a skill
    invoked by a name nobody can find.
    """
    findings: list[str] = []
    skills = root / SKILLS_DIR
    if not skills.is_dir():
        return findings
    for directory in sorted(p for p in skills.iterdir() if p.is_dir()):
        name = (directory / "SKILL.md").relative_to(root).as_posix()
        if not (directory / "SKILL.md").is_file():
            findings.append(f"{name}: missing — the skill directory has no SKILL.md")
            continue
        text = (directory / "SKILL.md").read_text(encoding="utf-8")
        parsed = parse_frontmatter(text)
        if parsed is None:
            findings.append(f"{name}: no `---` frontmatter, so the skill cannot load")
            continue
        fields, _ = parsed
        if _as_text(fields.get("name")) != directory.name:
            findings.append(
                f"{name}: frontmatter name `{_as_text(fields.get('name'))}` does not "
                f"match its directory `{directory.name}`"
            )
        description = _as_text(fields.get("description"))
        if not description.strip():
            findings.append(f"{name}: empty description, so the skill never fires")
        chars = len(description) + len(_as_text(fields.get("when_to_use")))
        if chars > MAX_SKILL_DESCRIPTION_CHARS:
            findings.append(
                f"{name}: description is {chars} characters, over the "
                f"{MAX_SKILL_DESCRIPTION_CHARS} the skill listing truncates at"
            )
        count = len(text.splitlines())
        if count >= MAX_SKILL_LINES:
            findings.append(
                f"{name}: {count} lines, at or over the {MAX_SKILL_LINES}-line skill "
                "budget — move detail into a supporting file beside SKILL.md or into the "
                "docstring of the code it describes"
            )
    return findings


def check_role_agents(root: Path) -> list[str]:
    """Assert every role agent is thin and preloads only skills that exist.

    Agents are deliberately thin: knowledge in an agent body is a Claude-only copy that
    Copilot never reads, so an agent carries a role and a ``skills:`` preload and the
    knowledge itself lives in the skills.
    """
    findings: list[str] = []
    for name in role_agent_files(root):
        path = root / name
        text = path.read_text(encoding="utf-8")
        parsed = parse_frontmatter(text)
        if parsed is None:
            findings.append(f"{name}: no `---` frontmatter, so the agent cannot load")
            continue
        fields, _ = parsed
        if _as_text(fields.get("name")) != path.stem:
            findings.append(
                f"{name}: frontmatter name `{_as_text(fields.get('name'))}` does not "
                f"match its file `{path.stem}`"
            )
        if not _as_text(fields.get("description")).strip():
            findings.append(f"{name}: empty description, so nothing delegates to it")
        count = len(text.splitlines())
        if count >= MAX_AGENT_LINES:
            findings.append(
                f"{name}: {count} lines, at or over the {MAX_AGENT_LINES}-line agent "
                "budget — agents stay thin; put the knowledge in a skill it preloads"
            )
        skills = fields.get("skills", [])
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]
        for skill in skills:
            if not (root / SKILLS_DIR / skill / "SKILL.md").is_file():
                findings.append(
                    f"{name}: preloads skill `{skill}`, which has no "
                    f"{SKILLS_DIR}/{skill}/SKILL.md"
                )
    return findings


def check_named_skills_and_agents(root: Path) -> list[str]:
    """Assert every skill an instruction file cites, and every promised role agent, exists.

    The skill and agent checks are discovery-only: they validate the files that are
    there, so on their own they pass when a file ``AGENTS.md`` points an agent at has
    been deleted, leaving the instructions naming nothing.
    """
    findings: list[str] = []
    for name in instruction_files(root):
        path = root / name
        if not path.is_file():
            continue  # already reported by check_paths
        prose, _ = split_fences(path.read_text(encoding="utf-8"))
        for span in sorted(set(backtick_spans(prose))):
            if not _SKILL_NAME_RE.match(span):
                continue
            if not (root / SKILLS_DIR / span / "SKILL.md").is_file():
                findings.append(
                    f"{name}: names skill `{span}`, which has no {SKILLS_DIR}/{span}/SKILL.md"
                )
    agents = root / "AGENTS.md"
    if not agents.is_file():
        return findings  # already reported by check_claude_import
    spans = set(backtick_spans(agents.read_text(encoding="utf-8")))
    for role in ROLE_AGENTS:
        if not (root / AGENTS_DIR / f"{role}.md").is_file():
            findings.append(
                f"{AGENTS_DIR}/{role}.md: missing — AGENTS.md promises this role agent"
            )
        if role not in spans:
            findings.append(
                f"AGENTS.md: no longer names role agent `{role}`; drop it from "
                "ROLE_AGENTS in check_agents_md.py or name it again"
            )
    return findings


def run_checks(root: Path) -> list[str]:
    """Run every check and return all findings, not merely the first."""
    findings: list[str] = []
    for check in (
        check_claude_import,
        check_copilot_pointer,
        check_nested_shims,
        check_eager_budget,
        check_scoped_budgets,
        check_skills,
        check_role_agents,
        check_named_skills_and_agents,
        check_paths,
        check_env_names,
    ):
        findings.extend(check(root))
    return findings


def annotate(root: Path, findings: list[str]) -> bool:
    """Rewrite the drift-report region of ``AGENTS.md``; return True if it changed.

    The region is an HTML comment because block-level HTML comments are stripped
    before injection into an agent's context: the report is visible to a human in the
    PR diff and costs a reading agent nothing.
    """
    path = root / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    if DRIFT_START not in text or DRIFT_END not in text:
        raise SystemExit(
            f"AGENTS.md has no {DRIFT_START} / {DRIFT_END} region for --annotate to write to"
        )
    body = "\n".join([DRIFT_START] + [f"- {finding}" for finding in findings])
    replacement = f"{body}\n{DRIFT_END}"
    start = text.index(DRIFT_START)
    end = text.index(DRIFT_END) + len(DRIFT_END)
    updated = text[:start] + replacement + text[end:]
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    """Entry point.  Returns a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--annotate",
        action="store_true",
        help="write findings into the AGENTS.md drift-report region instead of failing",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    findings = run_checks(root)

    if args.annotate:
        changed = annotate(root, findings)
        print(
            f"{len(findings)} finding(s); drift report "
            f"{'updated' if changed else 'already current'}"
        )
        return 0

    if findings:
        print(f"{len(findings)} instruction-file problem(s):", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1

    print("Instruction files check out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
