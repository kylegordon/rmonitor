"""Tests for the agent-instruction freshness check.

The script lives in ``.github/scripts/`` rather than an importable package, and this
repository has no ``conftest.py`` to put it on the path, so it is loaded by file
location.  Every test builds its own miniature repository under ``tmp_path``, so each
check's behaviour is pinned independently of what the real files happen to say. The
real files are checked too, by ``test_the_instruction_files_pass_their_own_check`` in
``tests/test_repo_invariants.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_agents_md.py"
_spec = importlib.util.spec_from_file_location("check_agents_md", _SCRIPT)
assert _spec is not None and _spec.loader is not None
check_agents_md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_agents_md)


AGENTS_BODY = """# AGENTS.md — fixture

Run the suite with `REQUIRE_DISPLAY=1`. The relay entry point is `relay/main.py` and
the dependencies live in `relay/requirements.txt`.
Role agents: `programmer`, `tester`, `docs-keeper`, `reviewer`.

<!-- drift-report:start -->
<!-- drift-report:end -->
"""

CLAUDE_BODY = """# CLAUDE.md — fixture

@AGENTS.md

Claude-specific notes go here.
"""

COPILOT_BODY = """# Copilot Instructions

The canonical instructions are in `AGENTS.md`.
"""


def make_repo(root: Path) -> Path:
    """Build a minimal, passing instruction-file fixture under *root*."""
    (root / ".github").mkdir(parents=True, exist_ok=True)
    (root / "relay").mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(AGENTS_BODY, encoding="utf-8")
    (root / "CLAUDE.md").write_text(CLAUDE_BODY, encoding="utf-8")
    (root / ".github" / "copilot-instructions.md").write_text(COPILOT_BODY, encoding="utf-8")
    (root / "relay" / "requirements.txt").write_text("aiohttp>=3.9,<4\n", encoding="utf-8")
    (root / "relay" / "main.py").write_text(
        'import os\n\nREQUIRE_DISPLAY = os.environ.get("REQUIRE_DISPLAY", "0")\n',
        encoding="utf-8",
    )
    (root / ".claude" / "skills" / "rmonitor-base").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "skills" / "rmonitor-base" / "SKILL.md").write_text(
        "---\nname: rmonitor-base\ndescription: Use when testing.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (root / ".claude" / "agents").mkdir(parents=True, exist_ok=True)
    for role in check_agents_md.ROLE_AGENTS:
        (root / ".claude" / "agents" / f"{role}.md").write_text(
            f"---\nname: {role}\ndescription: Use for testing.\nskills:\n  - rmonitor-base\n"
            "---\n\nFollow AGENTS.md.\n",
            encoding="utf-8",
        )
    return root


def test_clean_fixture_produces_no_findings(tmp_path: Path) -> None:
    assert check_agents_md.run_checks(make_repo(tmp_path)) == []


def test_missing_path_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text(
        AGENTS_BODY + "\nSee `relay/does_not_exist.py` for details.\n", encoding="utf-8"
    )
    findings = check_agents_md.run_checks(root)
    assert any("relay/does_not_exist.py" in f for f in findings)


def test_unknown_environment_variable_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text(
        AGENTS_BODY + "\nSet `INVENTED_SETTING` before running.\n", encoding="utf-8"
    )
    findings = check_agents_md.run_checks(root)
    assert any("INVENTED_SETTING" in f for f in findings)


def test_removed_claude_import_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "CLAUDE.md").write_text("# CLAUDE.md\n\nNo import here.\n", encoding="utf-8")
    findings = check_agents_md.run_checks(root)
    assert any("@AGENTS.md" in f for f in findings)


def test_import_inside_a_code_fence_does_not_count(tmp_path: Path) -> None:
    """A fenced ``@AGENTS.md`` is not parsed as an import by Claude Code either."""
    root = make_repo(tmp_path)
    (root / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\n```\n@AGENTS.md\n```\n", encoding="utf-8"
    )
    findings = check_agents_md.run_checks(root)
    assert any("@AGENTS.md" in f for f in findings)


def test_regrown_copilot_pointer_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    body = "# Copilot Instructions\n\nSee `AGENTS.md`.\n" + "\nfiller\n" * 40
    (root / ".github" / "copilot-instructions.md").write_text(body, encoding="utf-8")
    findings = check_agents_md.run_checks(root)
    assert any("pointer budget" in f for f in findings)


def test_copilot_pointer_that_stops_pointing_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / ".github" / "copilot-instructions.md").write_text(
        "# Copilot Instructions\n\nNothing here.\n", encoding="utf-8"
    )
    findings = check_agents_md.run_checks(root)
    assert any("does not name AGENTS.md" in f for f in findings)


def test_overlong_instruction_set_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text(
        AGENTS_BODY + "filler\n" * check_agents_md.MAX_EAGER_LINES, encoding="utf-8"
    )
    findings = check_agents_md.run_checks(root)
    assert any("line budget" in f for f in findings)


def test_lines_moved_behind_an_import_still_count(tmp_path: Path) -> None:
    """The budget is on the eagerly loaded set, so an import is not an escape hatch.

    Claude Code resolves ``@`` imports up front.  A split that satisfies a per-file
    limit while leaving the same text in front of the agent has to keep failing, or the
    check measures filing rather than attention.
    """
    root = make_repo(tmp_path)
    filler = "filler\n" * check_agents_md.MAX_EAGER_LINES
    (root / "overflow.md").write_text(filler, encoding="utf-8")
    (root / "AGENTS.md").write_text(AGENTS_BODY + "\n@overflow.md\n", encoding="utf-8")

    findings = check_agents_md.run_checks(root)

    assert any("line budget" in f for f in findings)
    assert any("overflow.md" in f for f in findings), "the breakdown must name the file"


def test_drift_report_region_is_not_counted_against_the_budget(tmp_path: Path) -> None:
    """A drift report must not manufacture a length finding on top of the real one."""
    root = make_repo(tmp_path)
    report = ["- " + "x" * 40] * check_agents_md.MAX_EAGER_LINES
    (root / "AGENTS.md").write_text(
        AGENTS_BODY.replace(
            check_agents_md.DRIFT_START,
            check_agents_md.DRIFT_START + "\n" + "\n".join(report),
        ),
        encoding="utf-8",
    )

    assert not any("line budget" in f for f in check_agents_md.run_checks(root))


def _add_scoped(root: Path, *, shim: str | None = "# CLAUDE.md — server/\n\n@AGENTS.md\n") -> Path:
    """Give *root* a ``server/AGENTS.md``, optionally with its ``CLAUDE.md`` shim."""
    (root / "server").mkdir(parents=True, exist_ok=True)
    (root / "server" / "AGENTS.md").write_text(
        "# server/AGENTS.md\n\nSee `relay/main.py` for the environment idiom.\n",
        encoding="utf-8",
    )
    if shim is not None:
        (root / "server" / "CLAUDE.md").write_text(shim, encoding="utf-8")
    return root


def test_scoped_agents_md_with_its_shim_is_clean(tmp_path: Path) -> None:
    assert check_agents_md.run_checks(_add_scoped(make_repo(tmp_path))) == []


def test_scoped_agents_md_without_a_shim_is_reported(tmp_path: Path) -> None:
    """Without the shim, Copilot obeys the scoped rules and Claude Code never sees them."""
    root = _add_scoped(make_repo(tmp_path), shim=None)

    findings = check_agents_md.run_checks(root)

    assert any("server/CLAUDE.md" in f and "missing" in f for f in findings)


def test_scoped_shim_that_does_not_import_is_reported(tmp_path: Path) -> None:
    root = _add_scoped(make_repo(tmp_path), shim="# CLAUDE.md — server/\n\nNotes.\n")

    findings = check_agents_md.run_checks(root)

    assert any("server/CLAUDE.md" in f and "import" in f for f in findings)


def test_overlong_scoped_agents_md_is_reported(tmp_path: Path) -> None:
    root = _add_scoped(make_repo(tmp_path))
    (root / "server" / "AGENTS.md").write_text(
        "filler\n" * check_agents_md.MAX_SCOPED_LINES, encoding="utf-8"
    )

    findings = check_agents_md.run_checks(root)

    assert any("server/AGENTS.md" in f and "scoped budget" in f for f in findings)


def test_stale_path_in_a_scoped_file_is_reported(tmp_path: Path) -> None:
    """Scoped files are discovered, so they are checked without being registered."""
    root = _add_scoped(make_repo(tmp_path))
    (root / "server" / "AGENTS.md").write_text(
        "# server/AGENTS.md\n\nSee `server/does_not_exist.py`.\n", encoding="utf-8"
    )

    findings = check_agents_md.run_checks(root)

    assert any("server/does_not_exist.py" in f for f in findings)


def test_git_refs_and_protocol_fields_are_not_mistaken_for_references(tmp_path: Path) -> None:
    """`origin/master` is a ref and `$E TRACKLENGTH` a message field, not a path or var."""
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text(
        AGENTS_BODY + "\nBranch from `origin/master`; read `$E TRACKLENGTH` from the feed.\n",
        encoding="utf-8",
    )
    assert check_agents_md.run_checks(root) == []


def test_allowlisted_paths_are_not_required_to_exist(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "CLAUDE.md").write_text(
        CLAUDE_BODY + "\nArtifacts live in `.rpi-tracking/`, which is un-versioned.\n",
        encoding="utf-8",
    )
    assert check_agents_md.run_checks(root) == []


def test_annotate_writes_findings_then_clears_them(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    agents = root / "AGENTS.md"
    agents.write_text(
        AGENTS_BODY + "\nSee `relay/does_not_exist.py` for details.\n", encoding="utf-8"
    )

    findings = check_agents_md.run_checks(root)
    assert check_agents_md.annotate(root, findings) is True
    written = agents.read_text(encoding="utf-8")
    assert "relay/does_not_exist.py" in written.split(check_agents_md.DRIFT_START)[1].split(
        check_agents_md.DRIFT_END
    )[0]

    agents.write_text(AGENTS_BODY, encoding="utf-8")
    assert check_agents_md.run_checks(root) == []
    assert check_agents_md.annotate(root, []) is False
    region = agents.read_text(encoding="utf-8").split(check_agents_md.DRIFT_START)[1]
    assert region.split(check_agents_md.DRIFT_END)[0].strip() == ""


def test_annotate_without_a_drift_region_fails_loudly(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text("# AGENTS.md\n\nNo region here.\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        check_agents_md.annotate(root, [])


def _add_skill(
    root: Path,
    name: str,
    *,
    description: str = "Use when testing.",
    body: str = "Body.\n",
    frontmatter_name: str | None = None,
) -> Path:
    """Give *root* a ``.claude/skills/<name>/SKILL.md``; return its path."""
    directory = root / ".claude" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {frontmatter_name or name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )
    return path


def _add_agent(
    root: Path, name: str, *, skills: tuple[str, ...] = ("rmonitor-fixture",), extra_lines: int = 0
) -> Path:
    """Give *root* a ``.claude/agents/<name>.md`` preloading *skills*; return its path."""
    directory = root / ".claude" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    preload = "".join(f"  - {skill}\n" for skill in skills)
    path.write_text(
        f"---\nname: {name}\ndescription: Use for testing.\nskills:\n{preload}---\n\n"
        "Follow AGENTS.md.\n" + "filler\n" * extra_lines,
        encoding="utf-8",
    )
    return path


def test_clean_fixture_with_a_skill_and_an_agent_is_clean(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture")
    _add_agent(root, "reviewer")

    assert check_agents_md.run_checks(root) == []


def test_skill_whose_name_does_not_match_its_directory_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture", frontmatter_name="other-name")

    findings = check_agents_md.run_checks(root)

    assert any(".claude/skills/rmonitor-fixture/SKILL.md" in f and "name" in f for f in findings)


def test_skill_without_a_description_is_reported(tmp_path: Path) -> None:
    """A skill fires on its description alone, so an empty one never loads."""
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture", description="")

    findings = check_agents_md.run_checks(root)

    assert any(
        ".claude/skills/rmonitor-fixture/SKILL.md" in f and "description" in f for f in findings
    )


def test_skill_description_over_the_truncation_limit_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(
        root,
        "rmonitor-fixture",
        description="x" * (check_agents_md.MAX_SKILL_DESCRIPTION_CHARS + 1),
    )

    findings = check_agents_md.run_checks(root)

    assert any(
        ".claude/skills/rmonitor-fixture/SKILL.md" in f and "truncates" in f for f in findings
    )


def test_overlong_skill_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture", body="filler\n" * check_agents_md.MAX_SKILL_LINES)

    findings = check_agents_md.run_checks(root)

    assert any(
        ".claude/skills/rmonitor-fixture/SKILL.md" in f and "skill budget" in f for f in findings
    )


def test_skill_directory_without_skill_md_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / ".claude" / "skills" / "rmonitor-fixture").mkdir(parents=True)

    findings = check_agents_md.run_checks(root)

    assert any(".claude/skills/rmonitor-fixture/SKILL.md" in f and "missing" in f for f in findings)


def test_agent_preloading_a_missing_skill_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_agent(root, "reviewer", skills=("no-such-skill",))

    findings = check_agents_md.run_checks(root)

    assert any(
        ".claude/agents/reviewer.md" in f and "no-such-skill" in f for f in findings
    )


def test_overlong_agent_is_reported(tmp_path: Path) -> None:
    """Agents stay thin: knowledge in an agent body is a copy Copilot never reads."""
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture")
    _add_agent(root, "reviewer", extra_lines=check_agents_md.MAX_AGENT_LINES)

    findings = check_agents_md.run_checks(root)

    assert any(".claude/agents/reviewer.md" in f and "agent budget" in f for f in findings)


def test_agent_whose_name_does_not_match_its_file_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture")
    path = _add_agent(root, "reviewer")
    path.rename(path.with_name("renamed.md"))

    findings = check_agents_md.run_checks(root)

    assert any(".claude/agents/renamed.md" in f and "name" in f for f in findings)


def test_stale_path_in_a_skill_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture", body="See `server/does_not_exist.py`.\n")

    findings = check_agents_md.run_checks(root)

    assert any(
        ".claude/skills/rmonitor-fixture/SKILL.md" in f and "server/does_not_exist.py" in f
        for f in findings
    )


def test_unknown_environment_variable_in_a_skill_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture", body="Set `INVENTED_SETTING` first.\n")

    findings = check_agents_md.run_checks(root)

    assert any(
        ".claude/skills/rmonitor-fixture/SKILL.md" in f and "INVENTED_SETTING" in f
        for f in findings
    )


def test_frontmatter_parser_reads_scalars_and_both_list_forms() -> None:
    text = (
        "---\n"
        'name: "quoted-name"\n'
        "skills:\n"
        "  - one\n"
        "  - two\n"
        "paths: [a/*.py, b/**]\n"
        "---\n"
        "Body line.\n"
    )

    parsed = check_agents_md.parse_frontmatter(text)

    assert parsed is not None
    fields, body = parsed
    assert fields == {
        "name": "quoted-name",
        "skills": ["one", "two"],
        "paths": ["a/*.py", "b/**"],
    }
    assert body == "Body line."
    assert check_agents_md.parse_frontmatter("# No frontmatter\n") is None
    assert check_agents_md.parse_frontmatter("---\nname: unclosed\n") is None


def test_skill_and_agent_files_are_not_mistaken_for_scoped_agents_md(tmp_path: Path) -> None:
    """Neither a ``SKILL.md`` nor an agent file needs a ``CLAUDE.md`` shim beside it."""
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture")
    _add_agent(root, "reviewer")

    assert check_agents_md.scoped_agents_files(root) == []
    findings = check_agents_md.run_checks(root)
    assert not any("shim" in f or "scoped budget" in f for f in findings)


def test_stale_worktree_copies_are_not_checked(tmp_path: Path) -> None:
    """The harness leaves whole repository copies under ``.claude/worktrees/``."""
    root = make_repo(tmp_path)
    worktree = root / ".claude" / "worktrees" / "x"
    worktree.mkdir(parents=True)
    (worktree / "AGENTS.md").write_text("filler\n" * 100, encoding="utf-8")

    assert check_agents_md.run_checks(root) == []


@pytest.mark.parametrize("indicator", [">", ">-", "|", "|-", ">2", "|+", ">- # folded"])
def test_frontmatter_parser_reads_block_scalars(indicator: str) -> None:
    """A folded description must be read, or the truncation check counts its indicator."""
    text = f"---\ndescription: {indicator}\n  First line,\n  second line.\nname: x\n---\n"

    parsed = check_agents_md.parse_frontmatter(text)

    assert parsed is not None
    fields, _ = parsed
    separator = "\n" if indicator.startswith("|") else " "
    assert fields == {"description": f"First line,{separator}second line.", "name": "x"}


def test_frontmatter_parser_keeps_colons_inside_a_value() -> None:
    parsed = check_agents_md.parse_frontmatter("---\ndescription: Use when: testing\n---\n")

    assert parsed is not None
    assert parsed[0] == {"description": "Use when: testing"}


def test_long_folded_skill_description_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    path = _add_skill(root, "rmonitor-fixture")
    long_line = "x" * (check_agents_md.MAX_SKILL_DESCRIPTION_CHARS + 1)
    path.write_text(
        f"---\nname: rmonitor-fixture\ndescription: >-\n  {long_line}\n---\n\nBody.\n",
        encoding="utf-8",
    )

    findings = check_agents_md.run_checks(root)

    assert any(
        ".claude/skills/rmonitor-fixture/SKILL.md" in f and "truncates" in f for f in findings
    )


def test_deleted_role_agent_is_reported(tmp_path: Path) -> None:
    """Discovery alone validates only the files left, so a deletion has to be caught."""
    root = make_repo(tmp_path)
    (root / ".claude" / "agents" / "reviewer.md").unlink()

    findings = check_agents_md.run_checks(root)

    assert any(".claude/agents/reviewer.md" in f and "missing" in f for f in findings)


def test_role_agent_dropped_from_agents_md_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text(
        AGENTS_BODY.replace(", `reviewer`", ""), encoding="utf-8"
    )

    findings = check_agents_md.run_checks(root)

    assert any("AGENTS.md" in f and "`reviewer`" in f for f in findings)


def test_cited_skill_that_does_not_exist_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text(
        AGENTS_BODY + "\nLoad `rmonitor-deleted` first.\n", encoding="utf-8"
    )

    findings = check_agents_md.run_checks(root)

    assert any("AGENTS.md" in f and "rmonitor-deleted" in f for f in findings)


def test_cited_skill_that_exists_is_clean(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-present")
    (root / "AGENTS.md").write_text(
        AGENTS_BODY + "\nLoad `rmonitor-present` first.\n", encoding="utf-8"
    )

    assert check_agents_md.run_checks(root) == []


def test_user_level_rpi_skills_may_be_named(tmp_path: Path) -> None:
    """Only ``rpi-artifacts`` is this repository's; the other RPI phase skills are not."""
    root = make_repo(tmp_path)
    (root / "AGENTS.md").write_text(
        AGENTS_BODY + "\nRun `rpi-plan` after `rpi-research`.\n", encoding="utf-8"
    )

    assert check_agents_md.run_checks(root) == []


def test_role_agent_missing_from_the_role_list_is_reported(tmp_path: Path) -> None:
    """A fifth role must be added to the checker's list and to AGENTS.md together."""
    root = make_repo(tmp_path)
    _add_skill(root, "rmonitor-fixture")
    _add_agent(root, "unlisted")

    findings = check_agents_md.run_checks(root)

    assert any(".claude/agents/unlisted.md" in f and "ROLE_AGENTS" in f for f in findings)


def test_skill_paths_pattern_matching_nothing_is_reported(tmp_path: Path) -> None:
    """A renamed file leaves a ``paths`` pattern that never auto-loads the skill."""
    root = make_repo(tmp_path)
    path = _add_skill(root, "rmonitor-fixture")
    path.write_text(
        "---\nname: rmonitor-fixture\ndescription: Use when testing.\npaths:\n"
        '  - relay/main.py\n  - "relay/*.txt"\n  - server/gone.py\n---\n\nBody.\n',
        encoding="utf-8",
    )

    findings = check_agents_md.run_checks(root)

    assert [f for f in findings if "paths" in f] == [
        ".claude/skills/rmonitor-fixture/SKILL.md: `paths` pattern `server/gone.py` "
        "matches no file, so the skill never auto-loads for it"
    ]


def test_skill_outside_the_naming_convention_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    _add_skill(root, "other-skill")

    findings = check_agents_md.run_checks(root)

    assert any(".claude/skills/other-skill/SKILL.md" in f and "rmonitor-*" in f for f in findings)


def test_a_comment_needs_whitespace_to_start_a_block_scalar() -> None:
    """``|#x`` is a plain scalar in YAML, not a block header with a comment."""
    parsed = check_agents_md.parse_frontmatter("---\ndescription: |#x\n---\n")

    assert parsed is not None
    assert parsed[0] == {"description": "|#x"}


def test_skill_paths_pattern_matching_only_an_empty_directory_is_reported(
    tmp_path: Path,
) -> None:
    root = make_repo(tmp_path)
    (root / "empty").mkdir()
    path = _add_skill(root, "rmonitor-fixture")
    path.write_text(
        "---\nname: rmonitor-fixture\ndescription: Use when testing.\npaths:\n"
        '  - "empty/**"\n---\n\nBody.\n',
        encoding="utf-8",
    )

    findings = check_agents_md.run_checks(root)

    assert any("`empty/**`" in f for f in findings)


@pytest.mark.parametrize("skills", ["", "skills: []\n"])
def test_agent_preloading_no_skills_is_reported(tmp_path: Path, skills: str) -> None:
    """A thin role with no preload carries none of the knowledge it exists to apply."""
    root = make_repo(tmp_path)
    (root / ".claude" / "agents" / "reviewer.md").write_text(
        f"---\nname: reviewer\ndescription: Use for testing.\n{skills}---\n\nBody.\n",
        encoding="utf-8",
    )

    findings = check_agents_md.run_checks(root)

    assert any(".claude/agents/reviewer.md" in f and "no skills" in f for f in findings)
