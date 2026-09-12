"""Executable versions of the rules in ``AGENTS.md``.

Every line of code here is agent-authored, so a rule that exists only as prose has
nothing enforcing it — an agent that misses the line breaks the rule silently and CI
stays green.  Each test below is the safety net under one such rule, named in the
docstring, and its existence is what allows the prose entry to be retired.

Kept deliberately independent of the packages under test: these assert facts about the
repository's shape, not about its runtime behaviour.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Requirements files the test image installs from, each in its own directory.
DECLARED_REQUIREMENTS = (
    "requirements-dev.txt",
    "relay/requirements.txt",
    "relay/requirements-build.txt",
    "server/requirements.txt",
)

#: Top-level packages that are this repository rather than a dependency.
FIRST_PARTY = frozenset({"relay", "server", "tests"})

#: Source trees whose imports must be declared.
SOURCE_TREES = ("relay", "server", "tests")

_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "build", "dist"}

#: A ``pip install`` naming packages rather than reading a requirements file.
_INLINE_PIP_RE = re.compile(r"pip\s+install\s+(?!.*(?:-r|--requirement)\b)(?P<rest>\S.*)")


def normalise(name: str) -> str:
    """Return *name* in the form that compares a module to a distribution."""
    return name.lower().replace("_", "-")


def declared_packages() -> set[str]:
    """Return every distribution named in the installed requirements files."""
    names: set[str] = set()
    for rel in DECLARED_REQUIREMENTS:
        for line in (ROOT / rel).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                names.add(normalise(re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()))
    return names


def source_files() -> list[Path]:
    """Return every Python file in the trees whose imports must be declared."""
    return [
        path
        for tree in SOURCE_TREES
        for path in sorted((ROOT / tree).rglob("*.py"))
        if not set(path.relative_to(ROOT).parts) & _SKIP_DIRS
    ]


def imported_packages() -> dict[str, set[str]]:
    """Map each third-party top-level module to the files importing it."""
    found: dict[str, set[str]] = {}
    for path in source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
        for module in modules:
            if module in sys.stdlib_module_names or module in FIRST_PARTY:
                continue
            found.setdefault(module, set()).add(path.relative_to(ROOT).as_posix())
    return found


def workflow_files() -> list[Path]:
    """Return every GitHub Actions workflow."""
    return sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_every_third_party_import_is_declared_in_an_installed_requirements_file() -> None:
    """Guards AGENTS.md: dependencies belong in a ``requirements*.txt``.

    This is the check that was missing when ``relay/gui.py`` began importing
    ``platformdirs``: the package was declared in a build-only requirements file the
    test job did not install, so two test modules failed collection with
    ``ModuleNotFoundError`` while the release and publish workflows stayed green.
    """
    declared = declared_packages()
    undeclared = {
        module: sorted(files)
        for module, files in imported_packages().items()
        if normalise(module) not in declared
    }
    assert not undeclared, (
        "imported but not declared in any requirements file the test image installs: "
        f"{undeclared}"
    )


@pytest.mark.parametrize("workflow", workflow_files(), ids=lambda p: p.name)
def test_no_workflow_installs_packages_inline(workflow: Path) -> None:
    """Guards AGENTS.md: never add a dependency to a workflow's ``pip install``.

    Fixing one workflow's inline list leaves every other one to drift out of sync on
    its own schedule, which is precisely how the failure above went unnoticed.
    """
    offenders = [
        line.strip()
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and _INLINE_PIP_RE.search(line)
    ]
    assert not offenders, (
        f"{workflow.relative_to(ROOT)} installs packages inline: {offenders}. "
        "Add them to the scope-matching requirements*.txt and install with -r."
    )


def test_requirements_files_land_at_distinct_paths_in_the_test_image() -> None:
    """Guards AGENTS.md: the requirements files must keep their paths in the image.

    ``relay/requirements.txt`` and ``server/requirements.txt`` share a basename, so a
    COPY naming all four into one flat destination lands three files, not four —
    silently, with no build error, and harmless only while those two stay identical.
    """
    dockerfile = (ROOT / "tests" / "Dockerfile").read_text(encoding="utf-8")
    for rel in DECLARED_REQUIREMENTS:
        assert f"-r {rel}" in dockerfile, f"{rel} is not installed by the test image"

    landed: dict[str, str] = {}
    for sources, destination in re.findall(r"^COPY\s+(.+?)\s+(\S+)\s*$", dockerfile, re.M):
        for source in sources.split():
            if source.endswith(".txt"):
                landed[source] = os.path.normpath(f"{destination}/{Path(source).name}")

    missing = [rel for rel in DECLARED_REQUIREMENTS if rel not in landed]
    assert not missing, f"never COPYed into the test image: {missing}"
    assert len(set(landed.values())) == len(landed), (
        f"two requirements files land on the same path: {landed}"
    )
    for rel in DECLARED_REQUIREMENTS:
        assert landed[rel] == os.path.normpath(rel), (
            f"{rel} lands at {landed[rel]}, but the -r line in the same file reads {rel}"
        )


def test_xvfb_run_is_never_invoked() -> None:
    """Guards AGENTS.md: use ``xvfb-run`` here — never.

    Its wait-for-display poll has hung indefinitely in this repository twice.
    ``tests/entrypoint.sh`` starts Xvfb directly instead. Prose is allowed to name the
    wrapper in order to forbid it; an executable line may not invoke it. This file is
    excluded because stating the rule requires spelling it.
    """
    executable = [
        path
        for pattern in ("*.sh", "*.yml", "*.yaml", "*.py")
        for path in sorted(ROOT.rglob(pattern))
        if not set(path.relative_to(ROOT).parts) & _SKIP_DIRS
        and path.resolve() != Path(__file__).resolve()
    ]
    offenders = []
    for path in executable:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if "xvfb-run" in line and not stripped.startswith(("#", '"""', "*")):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert not offenders, f"xvfb-run invoked at {offenders}"


def test_there_is_no_conftest_py() -> None:
    """Guards tests/AGENTS.md: there is no ``conftest.py`` and no global asyncio mode.

    Every async test carries its own ``pytest.mark.asyncio`` because of it. Adding one
    would make those markers look redundant and invite their removal, so the absence is
    a fact worth asserting rather than a gap.
    """
    found = [
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("conftest.py")
        if not set(path.relative_to(ROOT).parts) & _SKIP_DIRS
    ]
    assert not found, f"conftest.py appeared at {found}; see tests/AGENTS.md"


def test_the_page_reads_sort_mode_and_does_not_re_derive_it() -> None:
    """Guards server/AGENTS.md: ``index.html`` reads ``sort_mode``, never re-derives it.

    Which order the rows are in decides what the ``POS`` column means, and the server
    already answers that in the payload. If the template recomputed the condition from
    ``session_mode`` and ``flag`` instead, the server's branch would exist in two
    places in two languages, and only one of them is reachable from a test — nothing in
    this repository renders the template.

    So this asserts the textual half of the rule, which is the half that can be
    asserted: the page consults ``data.sort_mode``, and no sort condition in it is
    keyed on the session mode. What the page then *draws* — the row index, the tooltip,
    the suppressed arrows — is still unguarded, and closing that needs the template
    smoke test this repository does not yet have.
    """
    page = ROOT / "server" / "templates" / "index.html"
    source = page.read_text(encoding="utf-8")

    assert "data.sort_mode" in source, (
        "index.html no longer reads data.sort_mode; either the payload field was "
        "dropped or the page went back to deriving the sort itself"
    )

    # A sort decision keyed on the mode label rather than on the server's answer.
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), 1)
        if re.search(r"(session_mode|\bmode\b)\s*===?\s*['\"](Practice|Qualifying)", line)
    ]
    assert not offenders, (
        "index.html derives a sort condition from the session mode instead of reading "
        f"data.sort_mode: {offenders}"
    )
