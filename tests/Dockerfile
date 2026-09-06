# The single definition of this project's test environment, used by ./test.sh
# locally and by .github/workflows/tests.yml in CI. Build context is the repo root.
ARG PYTHON_VERSION=3.12
# -slim, not the full python:<version> image: the full image bundles tkinter and
# hides a missing libtk8.6, which is byte-for-byte the failure CI hit. The slim
# base is what makes a local run reproduce CI.
FROM python:${PYTHON_VERSION}-slim

# libtk8.6, not python3-tk: python3-tk is tkinter for the distro's python3 and
# satisfies this interpreter only by pulling libtk8.6 in transitively.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends libtk8.6 xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/app

# Paths preserved deliberately: relay/requirements.txt and server/requirements.txt
# share a basename, so copying all four into one flat destination directory lands
# three files, not four — silently, with no build error.
COPY requirements-dev.txt ./
COPY relay/requirements.txt relay/requirements-build.txt relay/
COPY server/requirements.txt server/
RUN pip install --no-cache-dir \
    -r requirements-dev.txt \
    -r relay/requirements.txt \
    -r relay/requirements-build.txt \
    -r server/requirements.txt

COPY tests/entrypoint.sh /usr/local/bin/entrypoint.sh

# No source is copied: tests/test_integration.py resolves examples/ from __file__,
# so the repo arrives by bind mount and source edits never invalidate a layer.

# REQUIRE_DISPLAY=1 — this image always has a display, so a missing one is a real
#   fault rather than a reason for tests/test_gui.py to skip.
# PYTHONDONTWRITEBYTECODE=1 — keeps __pycache__ out of the bind-mounted repo.
# HOME=/tmp — ./test.sh runs as the host uid, which has no passwd entry and so no
#   writable home; without this the run fails rather than merely warns.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REQUIRE_DISPLAY=1 \
    HOME=/tmp

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["tests/", "-v"]
