#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.herald.yml"
SERVICE="herald"
IMAGE="agent-core/watcher-pilot:latest"

# shellcheck source=_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

sandbox_guard_creds
sandbox_pick_docker
sandbox_ensure_docker

cd "${REPO_DIR}"

if [[ "$(sandbox_need_build "${IMAGE}" "${SCRIPT_DIR}/Dockerfile")" == "1" ]]; then
  env -i \
    HOME="${HOME}" \
    PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin" \
    DOCKER_HOST="${DOCKER_HOST:-}" \
    "${DOCKER}" compose -f "${COMPOSE_FILE}" build "${SERVICE}" >&2
else
  echo "[run-herald] image ${IMAGE} up to date with Dockerfile — skipping build" >&2
fi

if [[ $# -eq 0 ]]; then
  exec env -i \
    HOME="${HOME}" \
    PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin" \
    DOCKER_HOST="${DOCKER_HOST:-}" \
    "${DOCKER}" compose -f "${COMPOSE_FILE}" run --rm "${SERVICE}"
else
  exec env -i \
    HOME="${HOME}" \
    PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin" \
    DOCKER_HOST="${DOCKER_HOST:-}" \
    "${DOCKER}" compose -f "${COMPOSE_FILE}" run --rm --entrypoint "" "${SERVICE}" "$@"
fi
