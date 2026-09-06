#!/usr/bin/env python3
"""Validate that the agent instruction files still describe this repository.

Every line of code here is written by an agent, so ``AGENTS.md`` is the repository's
entire encoding of its engineering standards.  Before this check existed, the single
instruction file had drifted from the code in four measurable ways.  Three of those
were mechanically detectable, and this script detects them: it asserts that every
repo-relative path and every environment variable named in the instruction files is
real, that the ``@AGENTS.md`` import the no-duplication design rests on is intact,
that the Copilot pointer has not regrown into a second full copy, that every scoped
``AGENTS.md`` has the ``CLAUDE.md`` shim that makes Claude Code see it, and that the
instruction set an agent carries on every task stays inside its length budget.

Stdlib only, deliberately.  Adding a dependency for a documentation check would break
the very rule this file exists to protect (``AGENTS.md`` pitfall 11).

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
#: per-file limit while costing an agent exactly as much context as before.
#: https://code.claude.com/docs/en/memory targets "under 200 lines"; that number is a
#: proxy for an attention budget, so it has to be measured over everything actually in
#: front of the agent on every task, not over one file.
MAX_EAGER_LINES = 200

#: Budget for one scoped ``AGENTS.md``, loaded only when an agent works in that
#: directory.  Smaller than the eager budget on purpose: a scoped file needing more
#: than this is describing a subsystem, and the subsystem's own docstrings are where
#: that belongs.
MAX_SCOPED_LINES = 80

#: Maximum depth Claude Code follows ``@`` imports.
MAX_IMPORT_DEPTH = 5

#: The Copilot pointer must stay a pointer.  Copilot reads it *and* ``AGENTS.md`` with
#: no defined precedence, so a second full copy is genuinely undefined behaviour.
MAX_COPILOT_LINES = 20

#: Paths that legitimately do not exist in a checkout.
ALLOWED_MISSING_PATHS = frozenset(
    {
        # The RPI phase artifacts are deliberately local and git-ignored, so a CI
        # checkout never has them.  CLAUDE.md names the directory precisely to say
        # that committed docs must not cite paths inside it.
        ".rpi-tracking/",
        # AGENTS.md names conftest.py in order to say there isn't one, which is why
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

    Scoped files are discovered rather than listed, so a new ``server/AGENTS.md`` is
    checked for stale paths from the moment it is committed and nobody has to remember
    to register it here.
    """
    names = list(ROOT_INSTRUCTION_FILES)
    for path in sorted(root.rglob("AGENTS.md")) + sorted(root.rglob("CLAUDE.md")):
        rel = path.relative_to(root)
        if len(rel.parts) == 1 or set(rel.parts) & _SKIP_DIRS:
            continue
        names.append(rel.as_posix())
    return names


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


def run_checks(root: Path) -> list[str]:
    """Run every check and return all findings, not merely the first."""
    findings: list[str] = []
    for check in (
        check_claude_import,
        check_copilot_pointer,
        check_nested_shims,
        check_eager_budget,
        check_scoped_budgets,
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
