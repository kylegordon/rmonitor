#!/usr/bin/env bash
# Run the test suite in the container defined by tests/Dockerfile — the same image
# .github/workflows/tests.yml builds and runs.
#
#   ./test.sh                                    # whole suite
#   ./test.sh tests/test_gui.py -v               # one file
#   ./test.sh tests/test_gui.py::test_name -v    # one test
#   PYTHON_VERSION=3.13 ./test.sh                # the other interpreter CI gates
set -euo pipefail

# Exported, not just set: compose reads it from the environment for both the
# image: tag and the build arg.
export PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

# --build keeps a requirements*.txt change from being silently ignored; a warm
# rebuild costs about a quarter of a second.
#
# -T unconditionally: no test needs a TTY, and `docker compose run` allocates one
# by default, which errors in a non-TTY shell — exactly where an agent runs this.
# Unconditional -T makes the script behave identically in a terminal, an agent
# shell and CI. The trade-off is that pytest sees a non-tty and disables colour.
args=(--rm --build -T)

# Linux only: Docker Desktop's VM handles bind-mount ownership itself, and an
# explicit user there causes permission surprises rather than fixing them. On a
# native daemon it is what stops the container writing root-owned files into the
# working tree.
if [[ "$(uname -s)" == "Linux" ]]; then
  args+=(--user "$(id -u):$(id -g)")
fi

echo "==> Running tests on Python ${PYTHON_VERSION}..."
exec docker compose run "${args[@]}" tests "$@"
