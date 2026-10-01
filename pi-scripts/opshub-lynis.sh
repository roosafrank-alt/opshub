#!/bin/bash
# opshub-lynis.sh - weekly Lynis security audit for the OpsHub Pi (same idea as
# forge's). Installed to /usr/local/sbin/opshub-lynis by lynis-install.sh and run
# by opshub-lynis.timer (Saturdays 04:10 AM, low priority).
#
# Sends one phone push with the score. Louder (high priority) if the score drops
# by 3 or more points or any warning appears. Read-only: Lynis only looks.

STATE=/var/lib/opshub-security
LOGDIR=/var/log/opshub-lynis
NTFY_URL_FILE=/home/frank/.opshub-ntfy-url
R=/var/log/lynis-report.dat

mkdir -p "$STATE" "$LOGDIR"

notify() {  # title message priority tags
  [ -r "$NTFY_URL_FILE" ] || return 0
  url=$(tr -d '[:space:]' < "$NTFY_URL_FILE")
  [ -n "$url" ] || return 0
  curl -fsS -m 15 -H "Title: $1" -H "Priority: ${3:-default}" -H "Tags: ${4:-}" -d "$2" "$url" >/dev/null 2>&1 || true
}

out="$LOGDIR/lynis-$(date +%F).log"
lynis audit system --cronjob >"$out" 2>&1

score=$(grep -m1 '^hardening_index=' "$R" 2>/dev/null | cut -d= -f2)
if [ -z "$score" ]; then
  notify "OpsHub Lynis audit failed to run" "No score found. See $out" high warning
  exit 1
fi

warns=$(grep -c '^warning\[\]=' "$R")
sugg=$(grep -c '^suggestion\[\]=' "$R")
prev=$(cat "$STATE/lynis.score" 2>/dev/null || echo "$score")
echo "$score" > "$STATE/lynis.score"
touch "$STATE/lynis.ran"
find "$LOGDIR" -name 'lynis-*' -mtime +60 -delete 2>/dev/null

if [ "$score" -le $((prev-3)) ] || [ "$warns" -gt 0 ]; then
  notify "OpsHub security audit needs a look" "Score $score (was $prev), $warns warnings, $sugg suggestions. Warnings: $(grep '^warning\[\]=' "$R" | head -3 | cut -c1-120)" high warning
else
  notify "OpsHub weekly security audit" "Score $score (was $prev), $warns warnings, $sugg suggestions." low shield
fi
exit 0
