"""Behavioural tests for ``up.sh``, the deepcore deploy script.

Each test runs a copy of the script in a temporary directory with fake ``docker``,
``curl`` and ``sleep`` executables first on ``PATH``, so nothing touches the network
or a Docker daemon. A copy rather than the checkout's own file, because the
bind-mounted checkout may hold the user's real ``.env``, which the script sources.

The fakes append one line per call to ``calls.log``: ``docker|<IMAGE_VERSION>|<args>``
and ``curl|<url>``. Their behaviour is steered by ``FAKE_*`` environment variables.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

RELEASES_API = "https://api.github.com/repos/kylegordon/rmonitor/releases/latest"
HEALTHZ_URL = "https://timing.glasgownet.com/healthz"

FAKE_DOCKER = """#!/bin/sh
echo "docker|$IMAGE_VERSION|$*" >> "$CALLS_LOG"
case "$*" in
  *" pull "*) exit "${FAKE_PULL_STATUS:-0}" ;;
  inspect*) printf '%s\\n' "$FAKE_LABEL" ;;
esac
exit 0
"""

FAKE_CURL = """#!/bin/bash
url="${@: -1}"
echo "curl|$url" >> "$CALLS_LOG"
case "$url" in
  *releases/latest)
    printf '{\\n  "tag_name": "%s"\\n}\\n' "$FAKE_TAG"
    exit "${FAKE_API_STATUS:-0}" ;;
  *healthz)
    printf '%s' "$FAKE_HEALTHZ"
    exit "${FAKE_HEALTHZ_STATUS:-0}" ;;
esac
exit 99
"""


@pytest.fixture
def deploy(tmp_path):
    """Return a runner for ``up.sh`` in *tmp_path*, and the path of its call log.

    The default fakes describe a healthy deploy of release 0.1.18 whose ``/healthz``
    reports its version; each test overrides only what it is about.
    """
    shutil.copy(ROOT / "up.sh", tmp_path / "up.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("sleep", "#!/bin/sh\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    calls_log = tmp_path / "calls.log"

    def run(*args, **fakes):
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "CALLS_LOG": str(calls_log),
            "FAKE_TAG": "v0.1.18",
            "FAKE_LABEL": "0.1.18",
            "FAKE_HEALTHZ": '{"status": "ok", "version": "0.1.18"}',
        }
        env.update(fakes)
        return subprocess.run(
            ["bash", "up.sh", *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )

    return run, calls_log


def _calls(calls_log, prefix):
    if not calls_log.exists():
        return []
    return [line for line in calls_log.read_text().splitlines() if line.startswith(prefix)]


def test_no_argument_deploys_the_latest_release_pulling_before_starting(deploy):
    run, calls_log = deploy
    result = run()
    assert result.returncode == 0, result.stderr
    assert "Deploying server 0.1.18 (from latest release)" in result.stdout
    docker = _calls(calls_log, "docker|")
    pull = next(i for i, line in enumerate(docker) if " pull server" in line)
    up = next(i for i, line in enumerate(docker) if " up -d" in line)
    assert pull < up
    assert docker[pull].startswith("docker|0.1.18|")
    assert docker[up].startswith("docker|0.1.18|")
    assert not any("--build" in line for line in docker)


def test_an_argument_is_deployed_with_its_v_stripped_and_no_api_call(deploy):
    run, calls_log = deploy
    result = run("v0.1.17", FAKE_LABEL="0.1.17", FAKE_HEALTHZ='{"version": "0.1.17"}')
    assert result.returncode == 0, result.stderr
    assert "Deploying server 0.1.17 (from argument)" in result.stdout
    assert f"curl|{RELEASES_API}" not in _calls(calls_log, "curl|")


def test_an_image_version_pin_in_dotenv_is_used(deploy, tmp_path):
    run, _ = deploy
    (tmp_path / ".env").write_text("IMAGE_VERSION=0.1.16\n")
    result = run(FAKE_LABEL="0.1.16", FAKE_HEALTHZ='{"version": "0.1.16"}')
    assert result.returncode == 0, result.stderr
    assert "Deploying server 0.1.16 (from IMAGE_VERSION)" in result.stdout


def test_an_argument_wins_over_an_image_version_pin(deploy, tmp_path):
    run, _ = deploy
    (tmp_path / ".env").write_text("IMAGE_VERSION=0.1.16\n")
    result = run("0.1.17", FAKE_LABEL="0.1.17", FAKE_HEALTHZ='{"version": "0.1.17"}')
    assert result.returncode == 0, result.stderr
    assert "Deploying server 0.1.17 (from argument)" in result.stdout


@pytest.mark.parametrize("version", ["latest", "0.1"])
def test_a_version_that_is_not_a_full_release_is_rejected(deploy, version):
    run, calls_log = deploy
    result = run(version)
    assert result.returncode != 0
    assert _calls(calls_log, "docker|") == []


def test_two_arguments_print_usage_and_touch_nothing(deploy):
    run, calls_log = deploy
    result = run("0.1.17", "0.1.18")
    assert result.returncode == 2
    assert "Usage" in result.stderr
    assert _calls(calls_log, "docker|") == []


def test_a_failed_release_lookup_touches_nothing(deploy):
    run, calls_log = deploy
    result = run(FAKE_API_STATUS="22")
    assert result.returncode != 0
    assert _calls(calls_log, "docker|") == []


def test_a_failed_pull_stops_before_anything_is_restarted(deploy):
    run, calls_log = deploy
    result = run(FAKE_PULL_STATUS="1")
    assert result.returncode != 0
    assert "publish.yml" in result.stderr
    assert not any(" up " in line for line in _calls(calls_log, "docker|"))


@pytest.mark.parametrize("label", ["0.1.17", ""])
def test_a_running_label_other_than_the_target_fails(deploy, label):
    run, _ = deploy
    result = run(FAKE_LABEL=label)
    assert result.returncode != 0
    assert "rmonitor-server is running" in result.stderr


def test_a_healthz_without_a_version_is_a_note_not_a_failure(deploy):
    run, _ = deploy
    result = run(FAKE_HEALTHZ='{"status": "ok"}')
    assert result.returncode == 0, result.stderr
    assert "predates the /healthz version field" in result.stdout


def test_a_healthz_reporting_another_version_fails(deploy):
    run, _ = deploy
    result = run(FAKE_HEALTHZ='{"status": "ok", "version": "0.1.17"}')
    assert result.returncode != 0
    assert "reports 0.1.17" in result.stderr


def test_a_healthz_that_never_answers_fails_after_bounded_attempts(deploy):
    run, calls_log = deploy
    result = run(FAKE_HEALTHZ_STATUS="22")
    assert result.returncode != 0
    assert len(_calls(calls_log, f"curl|{HEALTHZ_URL}")) == 24


def test_a_successful_deploy_summarises_the_running_version(deploy):
    run, _ = deploy
    result = run()
    assert result.returncode == 0, result.stderr
    assert "Server 0.1.18 running" in result.stdout
    assert "predates" not in result.stdout
