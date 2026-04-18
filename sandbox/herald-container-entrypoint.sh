#!/bin/bash
# Runs INSIDE the herald sandbox container.
# Picks up the newest QC-eligible artifact in /brand-kit/inbox and invokes
# `python /app/herald qc <file>` — bypassing the hardcoded .venv shebang in
# ~/bin/herald since we call Python explicitly here.
#
# State file lives at /brand-kit/reports/.herald-inbox-last so last-seen
# survives across container runs (reports is RW-mounted).
#
# Exit code contract:
#   0 — Herald verdict was PASS or AUTO-FIXED (nothing to alert on)
#   1 — Herald verdict was BLOCKED (legitimate QC failure — review the report)
#   2 — Herald never emitted a verdict (SDK crash, import error, timeout)
# The log line `QC_RESULT=<verdict>` lets downstream alerting distinguish
# "QC is working, something failed review" from "Herald itself is broken".

set -u

INBOX="/brand-kit/inbox"
REPORTS="/brand-kit/reports"
STATE="${REPORTS}/.herald-inbox-last"
OUT="/tmp/herald-output.txt"

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
python /app/herald qc "$INBOX/$LATEST" 2>&1 | tee "$OUT"
PY_RC=${PIPESTATUS[0]}

VERDICT=""
if grep -qE '^HERALD: (PASS|AUTO-FIXED)\s*$' "$OUT"; then
    VERDICT=$(grep -oE 'HERALD: (PASS|AUTO-FIXED)' "$OUT" | tail -1 | cut -d: -f2 | tr -d ' ')
    RC=0
elif grep -qE '^HERALD: BLOCKED\s*$' "$OUT"; then
    VERDICT="BLOCKED"
    RC=1
else
    VERDICT="CRASH"
    RC=2
fi

echo "--- QC_RESULT=${VERDICT} py_rc=${PY_RC} exit_rc=${RC} ---"
echo "$LATEST" > "$STATE"
exit $RC
