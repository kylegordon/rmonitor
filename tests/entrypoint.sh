#!/usr/bin/env bash
# Container entrypoint for the test image: bring up a display, then hand every
# argument straight to pytest. Baked into the image so no invocation — local or
# CI — can forget the display and silently skip the GUI tests.
set -euo pipefail

# Started directly rather than via the xvfb-run wrapper, whose wait-for-display
# poll this repo has twice seen hang indefinitely.
Xvfb :99 -screen 0 1280x1024x24 &
for _ in $(seq 1 50); do
  [ -S /tmp/.X11-unix/X99 ] && break
  sleep 0.2
done
if [ ! -S /tmp/.X11-unix/X99 ]; then
  echo "Xvfb failed to start on :99" >&2
  exit 1
fi
export DISPLAY=:99

exec python -m pytest "$@"
