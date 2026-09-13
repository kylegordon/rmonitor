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


def _js_block(lines: list[str], opener: str) -> tuple[int, int]:
    """Line numbers, 1-based and inclusive, of the ``if`` block opened by *opener*.

    The template is asserted about as text -- nothing in this repository renders it --
    so a block is found by indentation: the sole line containing *opener*, through the
    next line that is a bare closing brace at the same indentation.

    :raises AssertionError: if *opener* does not appear on exactly one line.
    """
    starts = [number for number, line in enumerate(lines, 1) if opener in line]
    assert len(starts) == 1, f"expected one {opener!r} in index.html, found {starts}"
    start = starts[0]
    indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    end = next(
        number
        for number, line in enumerate(lines, 1)
        if number > start
        and line.strip() == "}"
        and len(line) - len(line.lstrip()) == indent
    )
    return start, end


def test_the_page_version_token_is_substituted_by_the_server() -> None:
    """Guards AGENTS.md pitfall 6: the two halves of the handshake share one spelling.

    The 2026-09-12 deploy shipped a matched server/page pair with nothing to make them
    run together, and a phone kept a tab open across it: the old page drew the feed's
    track position in the POS column while the new server sorted on best lap, so the
    numbers read out of sequence beside correct times. The fix is a version the server
    stamps into the page and repeats in every payload — which only works while both
    sides spell the token and the field identically, and neither spelling is reachable
    from the other's language.
    """
    page = (ROOT / "server" / "templates" / "index.html").read_text(encoding="utf-8")
    server = (ROOT / "server" / "server.py").read_text(encoding="utf-8")

    assert page.count("{{PAGE_VERSION}}") == 1, (
        "index.html must carry the version token exactly once; the server substitutes "
        "it at load and a second copy would go out unsubstituted"
    )
    assert server.count("{{PAGE_VERSION}}") == 1, (
        "server.py must name the version token exactly once, in PAGE_VERSION_TOKEN"
    )
    assert "msg.page_version" in page, (
        "index.html no longer reads msg.page_version; an outdated page can no longer "
        "tell that it is outdated"
    )
    assert '"page_version"' in server, (
        "server.py no longer sends a page_version field; the page has nothing to "
        "compare its own version against"
    )


def test_an_outdated_page_prompts_rather_than_reloading_itself() -> None:
    """Guards AGENTS.md pitfall 6: a stale page offers a reload, it never takes one.

    These displays run on users' own phones and laptops, not on unattended trackside
    screens, so someone is present to tap and an unrequested reload is worse than an
    offer. That is a decision about people rather than about code, which makes it
    exactly the kind nothing else in this repository can hold: no test here renders the
    template, so the guard is textual.

    The second half of this is subtler and was missed on first writing. A deploy changes
    the server process *and* the page, so the older ``server_instance_id`` guard fires on
    the same message as the version mismatch — and it reloads and returns. Written in
    that order the prompt is unreachable in precisely the case it exists for, and the
    page self-reloads after all. So the order and the ``!pageOutdated`` condition are
    both load-bearing, and both are asserted here.
    """
    page = (ROOT / "server" / "templates" / "index.html").read_text(encoding="utf-8")
    lines = page.splitlines()

    first, last = _js_block(lines, "if (pageOutdated)")
    body = "\n".join(lines[first - 1 : last])
    assert "classList.remove('hidden')" in body, (
        "the page_version mismatch branch must reveal the reload prompt"
    )
    assert "location.reload" not in body, (
        "an outdated page must prompt, not reload itself: these are users' own devices"
    )

    # The restart guard must yield to the prompt, and must be able to: `pageOutdated` is
    # read inside it, so it has to be computed above it.
    restart_first, restart_last = _js_block(lines, "msg.server_instance_id !== undefined")
    assert first < restart_first, (
        "the page_version check must run before the server_instance_id guard, or the "
        "guard reloads and returns before the prompt can appear"
    )

    deferral_first, deferral_last = _js_block(lines, "if (!pageOutdated)")
    assert restart_first < deferral_first <= deferral_last < restart_last, (
        "the !pageOutdated condition must sit inside the server_instance_id guard"
    )
    restart_reloads = [
        number
        for number in range(restart_first, restart_last + 1)
        if "location.reload" in lines[number - 1]
    ]
    assert restart_reloads, "the server_instance_id guard no longer reloads at all"
    assert all(deferral_first <= number <= deferral_last for number in restart_reloads), (
        "the server_instance_id guard reloads without checking !pageOutdated, so a "
        f"deploy reloads the page instead of prompting: {restart_reloads}"
    )

    # The user's tap is the *only* other way the page may reload, so exempt that exact
    # line rather than any listener: a `window.addEventListener('load', ...)` reload
    # would sail through a blanket exemption while being precisely what this forbids.
    taps = [
        number
        for number, line in enumerate(lines, 1)
        if "updateBannerEl.addEventListener('click'" in line
    ]
    assert len(taps) == 1, f"expected one banner click listener, found {taps}"
    assert "location.reload" in lines[taps[0] - 1], (
        "the banner's click listener no longer reloads, so tapping the prompt does "
        "nothing"
    )

    allowed = set(taps) | set(range(restart_first, restart_last + 1))
    unaccounted = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(lines, 1)
        if "location.reload" in line and number not in allowed
    ]
    assert not unaccounted, f"unexplained page reload at {unaccounted}"

    # The prompt has to reach a screen-reader user too: it is revealed rather than
    # inserted, so without a live region around it nothing announces that the data
    # being read is outdated.
    banner = next(number for number, line in enumerate(lines, 1) if 'id="update-banner"' in line)
    region = next(
        number
        for number, line in enumerate(lines, 1)
        if 'aria-live="polite"' in line and "role=\"status\"" in line
    )
    assert region < banner, (
        "the reload prompt must sit inside a persistent aria-live region, or its "
        "appearance is silent to assistive technology"
    )


#: A ``traefik.http.routers.<name>.<suffix>=<value>`` label line in the deploy compose file.
_ROUTER_LABEL = re.compile(r"^\s*-\s*traefik\.http\.routers\.([A-Za-z0-9_-]+)\.(\S+?)=(.*)$", re.M)

#: The middleware equivalent of :data:`_ROUTER_LABEL`.
_MIDDLEWARE_LABEL = re.compile(
    r"^\s*-\s*traefik\.http\.middlewares\.([A-Za-z0-9_-]+)\.(\S+?)=(.*)$", re.M
)

#: One ``Host(`name`)`` matcher inside a router rule.
_HOST_MATCHER = re.compile(r"Host\(`([^`]+)`\)")


def _compose_labels(pattern: re.Pattern[str]) -> dict[str, dict[str, str]]:
    """Traefik labels in ``docker-compose-deepcore.yaml``, grouped by router or middleware.

    Read as raw text rather than parsed as YAML on purpose: PyYAML is declared in none
    of the four requirements files, so importing it here would be caught by
    :func:`test_every_third_party_import_is_declared_in_an_installed_requirements_file`.
    The labels are flat ``key=value`` strings, so a regex loses nothing.

    :param pattern: :data:`_ROUTER_LABEL` or :data:`_MIDDLEWARE_LABEL`.
    :return: ``{name: {label_suffix: value}}``, e.g. ``{"timing": {"rule": "Host(...)"}}``.
    """
    text = (ROOT / "docker-compose-deepcore.yaml").read_text(encoding="utf-8")
    grouped: dict[str, dict[str, str]] = {}
    for name, suffix, value in pattern.findall(text):
        grouped.setdefault(name, {})[suffix] = value.strip()
    return grouped


def _rule_hosts(rule: str) -> frozenset[str]:
    """The set of hostnames a Traefik router *rule* matches on."""
    return frozenset(_HOST_MATCHER.findall(rule))


def test_every_tls_routed_host_has_an_http_to_https_redirect_router() -> None:
    """Guards the deploy compose file: every HTTPS host is also reachable over plain HTTP.

    The hostnames are written out twice — once on the HTTPS router, once on the HTTP
    router that redirects to it — and both sites are maintained by hand.  A host added
    to the first and missed on the second answers ``https://`` correctly while
    ``http://`` 404s, which is invisible to anyone who only ever types the scheme.  It
    also breaks the HTTP-01 ACME challenge, which is fetched over port 80.
    """
    routers = _compose_labels(_ROUTER_LABEL)
    redirectors = {
        name
        for name, labels in _compose_labels(_MIDDLEWARE_LABEL).items()
        if labels.get("redirectscheme.scheme") == "https"
    }
    assert redirectors, "no redirectscheme middleware is defined in the compose file"

    redirected: set[str] = set()
    for labels in routers.values():
        if labels.get("entrypoints") != "web":
            continue
        named = {part.strip() for part in labels.get("middlewares", "").split(",")}
        if named & redirectors:
            redirected |= _rule_hosts(labels.get("rule", ""))

    tls_routers = {
        name: _rule_hosts(labels.get("rule", ""))
        for name, labels in routers.items()
        if labels.get("tls") == "true"
    }
    assert tls_routers, "no TLS router is defined in the compose file"
    for name, hosts in tls_routers.items():
        assert hosts, f"router {name} enables TLS but names no Host()"
        missing = hosts - redirected
        assert not missing, (
            f"router {name} serves {sorted(missing)} over HTTPS, but no web-entrypoint "
            "router redirects those names — http:// will 404 and HTTP-01 cannot validate"
        )


def test_the_production_hostname_keeps_its_certificate_to_itself() -> None:
    """Guards the deploy compose file: one certificate per hostname group, never shared.

    Traefik derives one certificate per router from that router's ``Host()`` matchers,
    and HTTP-01 revalidates *every* identifier on a certificate at every renewal.  The
    two ``smart-timing.co.uk`` names reach this service by CNAME from a zone a third
    party owns, which we cannot change and will not be told about if they do.  Folding
    them onto the ``timing`` router puts all three names on one certificate, so a CNAME
    that is removed or repointed fails the whole ACME order — and
    ``timing.glasgownet.com`` stops renewing.  Production then goes dark roughly thirty
    days later, with nothing visibly broken in the interim.  Two routers, two orders,
    two blast radii.

    The resolvers must stay split for the same reason the routers do.  ``letsencrypt``
    on deepcore is DNS-01 through a Route 53 credential scoped to one hosted zone, so
    it cannot answer a challenge for ``smart-timing.co.uk`` at all; those names need an
    HTTP-01 resolver.  Tidying the second router onto its neighbour's resolver produces
    a configuration that deploys cleanly and then never obtains a certificate — silent
    in the repository and silent at runtime, which is why it is asserted here.
    """
    routers = _compose_labels(_ROUTER_LABEL)

    assert _rule_hosts(routers["timing"]["rule"]) == {"timing.glasgownet.com"}, (
        "the timing router must name the canonical hostname and nothing else; it is "
        f"currently {sorted(_rule_hosts(routers['timing']['rule']))}"
    )

    owner: dict[str, str] = {}
    for name, labels in routers.items():
        if labels.get("tls") != "true":
            continue
        for host in sorted(_rule_hosts(labels.get("rule", ""))):
            assert host not in owner, (
                f"{host} is served by both the {owner[host]} and {name} TLS routers, "
                "which couples their certificates' renewals together"
            )
            owner[host] = name

    assert routers["timing"]["tls.certresolver"] == "letsencrypt"
    assert routers["timing-smart"]["tls.certresolver"] == "letsencrypt-http", (
        "the smart-timing router must use the HTTP-01 resolver; the letsencrypt "
        "resolver is DNS-01 and cannot issue for a zone we do not control"
    )
