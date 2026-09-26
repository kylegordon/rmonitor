#!/usr/bin/env bash
# Deploy a released rmonitor server to deepcore (lodge.glasgownet.com), served by
# Traefik at https://timing.glasgownet.com (canonical), and the aliases
# https://live.smart-timing.co.uk and https://live-timing.smart-timing.co.uk.
#
# Usage:
#   ./up.sh           deploy the latest GitHub release
#   ./up.sh 0.1.17    deploy (or roll back to) that release
#
# The version comes from the argument, else IMAGE_VERSION (shell or .env), else the
# latest GitHub release. The image is pulled before anything is restarted, so a
# release with no published image aborts with nothing changed; afterwards the running
# container's version label and /healthz are checked against the target.
#
# Server only: relays are the GUI binaries, or the relay image run with
# docker-compose.yml (which IMAGE_VERSION pins too).
#
# Prerequisites:
#   - SSH access to bagpuss@lodge.glasgownet.com (key-based auth)
#   - RELAY_SECRET set in .env (or exported in your shell)
#   - /docker/timing/ directory created on deepcore
#   - curl on this machine
set -euo pipefail

DEPLOY_HOST="ssh://bagpuss@lodge.glasgownet.com"
COMPOSE_FILE="docker-compose-deepcore.yaml"
HEALTHZ_URL="https://timing.glasgownet.com/healthz"
RELEASES_API="https://api.github.com/repos/kylegordon/rmonitor/releases/latest"
# Traefik routes nothing to the new container until its health check passes, so allow
# about two minutes for /healthz to answer.
HEALTHZ_ATTEMPTS=24
HEALTHZ_INTERVAL=5

die() {
  echo "ERROR: $*" >&2
  exit 1
}

if (( $# > 1 )); then
  echo "Usage: $0 [VERSION]" >&2
  exit 2
fi

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if (( $# == 1 )); then
  version="$1"
  source_label="argument"
elif [[ -n "${IMAGE_VERSION:-}" ]]; then
  version="$IMAGE_VERSION"
  source_label="IMAGE_VERSION"
else
  if ! release_json=$(curl -fsSL "$RELEASES_API"); then
    die "could not query the latest release from $RELEASES_API"
  fi
  version=$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' <<<"$release_json" | head -n1)
  [[ -n "$version" ]] || die "no tag_name in the response from $RELEASES_API"
  source_label="latest release"
fi

# Release tags are v0.1.18; image tags are 0.1.18.
version="${version#v}"
# A full semver only: "latest" or "0.1" would move under us and defeat the pin.
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || die "'$version' is not a release version (expected e.g. 0.1.18)"

export IMAGE_VERSION="$version"
echo "==> Deploying server $version (from $source_label) to deepcore..."

# Pull first: compose's default pull_policy reuses any cached image, and a release can
# exist before (or without) its image, so a missing tag must stop us here.
if ! DOCKER_HOST="$DEPLOY_HOST" docker compose -f "$COMPOSE_FILE" pull server; then
  die "could not pull rmonitor-server:$version — if the release has no image, re-run" \
    "publish.yml for it from the release tag"
fi

DOCKER_HOST="$DEPLOY_HOST" docker compose -f "$COMPOSE_FILE" up -d

# The OCI label is set only by the release workflow, so it also rejects a locally
# built image.
if ! running=$(DOCKER_HOST="$DEPLOY_HOST" docker inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.version"}}' rmonitor-server); then
  running=""
fi
[[ "$running" == "$version" ]] \
  || die "rmonitor-server is running '${running:-no release label}', not $version"

healthy=0
for (( attempt = 1; attempt <= HEALTHZ_ATTEMPTS; attempt++ )); do
  if body=$(curl -fsS "$HEALTHZ_URL"); then
    healthy=1
    break
  fi
  if (( attempt < HEALTHZ_ATTEMPTS )); then
    sleep "$HEALTHZ_INTERVAL"
  fi
done
(( healthy )) || die "$HEALTHZ_URL did not answer after $HEALTHZ_ATTEMPTS attempts"

reported=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' <<<"$body" | head -n1)
if [[ -z "$reported" ]]; then
  echo "    Note: $version predates the /healthz version field; the label check stands."
elif [[ "$reported" != "$version" ]]; then
  die "$HEALTHZ_URL reports $reported, not $version"
fi

echo "==> Done. Server $version running"
echo "    Server:  https://timing.glasgownet.com"
echo "             https://live.smart-timing.co.uk"
echo "             https://live-timing.smart-timing.co.uk"
