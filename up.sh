#!/usr/bin/env bash
# Deploy rmonitor:
#   - server  → deepcore (lodge.glasgownet.com) via Traefik at https://timing.glasgownet.com
#   - relay   → local machine (connects to timing hardware, POSTs to deepcore server)
#
# Prerequisites:
#   - SSH access to bagpuss@lodge.glasgownet.com (key-based auth)
#   - RELAY_SECRET set in .env (or exported in your shell)
#   - SERVER_URL set in .env (or exported in your shell) — the compose-file
#     default (http://server:8080) targets a same-machine server container
#     that does not exist in this topology
#   - /docker/timing/ directory created on deepcore
set -euo pipefail

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "==> Deploying server to deepcore..."
DOCKER_HOST=ssh://bagpuss@lodge.glasgownet.com \
  docker compose -f docker-compose-deepcore.yaml up -d --build

echo "==> Deploying relay locally..."
docker compose up -d --build relay

echo "==> Done."
echo "    Server:  https://timing.glasgownet.com"
echo "    Relay:   running locally, posting to ${SERVER_URL:-<unset — check .env>}"
