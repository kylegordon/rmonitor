"""Tests for the agent-instruction freshness check.

The script lives in ``.github/scripts/`` rather than an importable package, and this
repository has no ``conftest.py`` to put it on the path, so it is loaded by file
location.  Every test builds its own miniature repository under ``tmp_path``: the
checks must be exercised against fixtures, not against the real instruction files,
or the suite would turn red every time the real documentation legitimately changed.
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
