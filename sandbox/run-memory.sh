#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is set in caller env. Refusing to launch sandbox." >&2
  echo "       These creds would break Max-subscription billing if they leaked in." >&2
  echo "       Unset it: \`unset ANTHROPIC_API_KEY\` and retry." >&2
  exit 2
fi
if [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
  echo "ERROR: ANTHROPIC_AUTH_TOKEN is set in caller env. Refusing to launch sandbox." >&2
  echo "       Unset it: \`unset ANTHROPIC_AUTH_TOKEN\` and retry." >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.memory.yml"
SERVICE="memory"

if ! command -v docker >/dev/null 2>&1; then
  DOCKER=/opt/homebrew/bin/docker
else
  DOCKER=$(command -v docker)
fi

cd "${REPO_DIR}"

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
