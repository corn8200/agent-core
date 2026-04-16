#!/bin/bash
# VPS data gathering script — runs on VPS via SSH
# Outputs structured text sections for parsing

echo '=== MAILQUEUE ==='
sqlite3 /srv/apps/sentry-mailqueue/queue.db "
SELECT status, count(*) FROM emails GROUP BY status;
" 2>/dev/null

echo '--- CLICKS ---'
sqlite3 /srv/apps/sentry-mailqueue/queue.db "
SELECT count(*) FROM events WHERE event_type='email.clicked';
" 2>/dev/null

echo '--- BOUNCES ---'
sqlite3 /srv/apps/sentry-mailqueue/queue.db "
SELECT count(*) FROM events WHERE event_type='email.bounced';
" 2>/dev/null

echo '--- RECENT_CLICKS ---'
sqlite3 /srv/apps/sentry-mailqueue/queue.db "
SELECT DISTINCT em.to_addr, e.timestamp FROM events e JOIN emails em ON e.email_id = em.id WHERE e.event_type='email.clicked' AND e.timestamp > datetime('now', '-3 days') ORDER BY e.timestamp DESC LIMIT 5;
" 2>/dev/null

echo '--- LEADS ---'
sqlite3 /srv/apps/sentry-mailqueue/queue.db "
SELECT status, count(*) FROM leads GROUP BY status;
" 2>/dev/null

echo '=== JOBSIGNAL ==='
sqlite3 /srv/apps/jobsignal/state/jobsignal.sqlite "
SELECT title, organization, primary_location, score_total, recommended_next_action FROM jobs WHERE recommended_next_action != 'Skip' AND first_seen > datetime('now', '-3 days') ORDER BY score_total DESC LIMIT 5;
" 2>/dev/null

echo '--- JOB_COUNTS ---'
sqlite3 /srv/apps/jobsignal/state/jobsignal.sqlite "
SELECT count(*) FROM jobs WHERE first_seen > datetime('now', '-3 days');
SELECT count(*) FROM jobs WHERE recommended_next_action != 'Skip' AND first_seen > datetime('now', '-3 days');
" 2>/dev/null

echo '=== INBOUND ==='
sqlite3 /srv/apps/mailgw/data/mailgw.db "SELECT from_addr, subject, classification, received_at FROM emails WHERE classification != 'junk' ORDER BY id DESC LIMIT 10;" 2>/dev/null

echo '=== FORMS ==='
sudo journalctl -u sentry-formhandler --since '72 hours ago' --no-pager -q 2>/dev/null | grep -i 'quote\|submission\|form' | tail -5

echo '=== SAM ==='
sudo journalctl -u sentry-sam-finder --since '7 days ago' --no-pager -q 2>/dev/null | grep -i 'match\|found\|bid' | tail -5

echo '=== HEALTH ==='
df -h / | tail -1
free -h | grep Mem
uptime

echo '=== SERVICES ==='
# Only long-running services (daemons). Timer-triggered oneshots are NORMALLY
# inactive between runs — checking them creates false positives.
# 2026-04-16: removed weather-poller, weather-app — services deleted from VPS,
# were triggering false anomaly alerts every 30 min.
for svc in sentry-dashboard sentry-formhandler sentry-mailqueue-web mailgw-idle mailgw-web caddy prepper-v2 jobsignal-dashboard; do
  status=$(systemctl is-active $svc 2>/dev/null)
  echo "$svc: $status"
done
# The one real timer we rely on:
echo "sentry-orchestrator.timer: $(systemctl is-active sentry-orchestrator.timer 2>/dev/null)"

echo '=== ERRORS ==='
sudo journalctl --priority=err --since '24 hours ago' --no-pager -q 2>/dev/null | tail -10

echo '=== END ==='
