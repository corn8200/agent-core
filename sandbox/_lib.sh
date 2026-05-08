#!/usr/bin/env bash
# Shared helpers for sandbox run-*.sh wrappers. Source this file; do not execute.
#
# Functions:
#   sandbox_guard_creds            — abort if ANTHROPIC_API_KEY/AUTH_TOKEN set
#   sandbox_pick_docker            — export DOCKER=<path to docker binary>
#   sandbox_ensure_docker          — require a reachable Docker daemon
#   sandbox_need_build <image>     — echo 1 if image missing OR Dockerfile newer; else 0

sandbox_guard_creds() {
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "ERROR: ANTHROPIC_API_KEY is set in caller env. Refusing to launch sandbox." >&2
    echo "       Unset it: \`unset ANTHROPIC_API_KEY\` and retry." >&2
    exit 2
  fi
  if [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    echo "ERROR: ANTHROPIC_AUTH_TOKEN is set in caller env. Refusing to launch sandbox." >&2
    echo "       Unset it: \`unset ANTHROPIC_AUTH_TOKEN\` and retry." >&2
    exit 2
  fi
}

sandbox_pick_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    DOCKER=/opt/homebrew/bin/docker
  else
    DOCKER=$(command -v docker)
  fi
  export DOCKER
}

sandbox_ensure_docker() {
  if "${DOCKER}" info >/dev/null 2>&1; then
    return 0
  fi
  echo "[sandbox] Docker daemon is not responding. Start Docker Desktop or another compatible daemon and retry." >&2
  return 1
}

sandbox_need_build() {
  local image="$1" dockerfile="$2"
  if ! "${DOCKER}" image inspect "$image" >/dev/null 2>&1; then
    echo 1
    return
  fi
  local img_created
  img_created=$("${DOCKER}" image inspect --format '{{.Created}}' "$image" 2>/dev/null | cut -c1-19)
  local img_epoch
  img_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S" "$img_created" +%s 2>/dev/null || echo 0)
  local df_mtime
  df_mtime=$(stat -f %m "$dockerfile" 2>/dev/null || echo 9999999999)
  if [[ $img_epoch -gt 0 && $df_mtime -lt $img_epoch ]]; then
    echo 0
  else
    echo 1
  fi
}
