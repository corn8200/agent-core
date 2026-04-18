#!/bin/bash
# Runs INSIDE the herald sandbox container.
# Picks up the newest QC-eligible artifact in /brand-kit/inbox and invokes
# `python /app/herald qc <file>` — bypassing the hardcoded .venv shebang in
# ~/bin/herald since we call Python explicitly here.
#
# State file lives at /brand-kit/reports/.herald-inbox-last so last-seen
# survives across container runs (reports is RW-mounted).

set -u

INBOX="/brand-kit/inbox"
REPORTS="/brand-kit/reports"
STATE="${REPORTS}/.herald-inbox-last"

[ -d "$INBOX" ] || { echo "herald-sandbox: $INBOX missing" >&2; exit 0; }
[ -d "$REPORTS" ] || mkdir -p "$REPORTS" 2>/dev/null || true

LATEST=$(ls -t "$INBOX" 2>/dev/null | grep -iE '\.(pdf|pptx|html|htm)$' | head -1 || true)
if [ -z "${LATEST:-}" ]; then
    echo "herald-sandbox: no eligible artifact in inbox"
    exit 0
fi

LAST_SEEN=""
[ -f "$STATE" ] && LAST_SEEN=$(cat "$STATE" 2>/dev/null || echo "")
if [ "$LATEST" = "$LAST_SEEN" ]; then
    echo "herald-sandbox: latest ($LATEST) already processed, skipping"
    exit 0
fi

echo "=== $(date -Iseconds) herald-sandbox: QC $LATEST ==="
if python /app/herald qc "$INBOX/$LATEST"; then
    echo "--- PASS ---"
    RC=0
else
    RC=$?
    echo "--- BLOCKED/FAIL rc=$RC ---"
fi

echo "$LATEST" > "$STATE"
exit $RC
