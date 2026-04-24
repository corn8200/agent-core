#!/bin/bash
set -e
cd ~/Projects/agent-core

if [[ -r "$HOME/.config/claude-oat01-shell-init.sh" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.config/claude-oat01-shell-init.sh" 2>/dev/null || true
fi

exec .venv/bin/python -m home_ops.engine "$@" 2>&1
