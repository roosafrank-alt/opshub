#!/bin/bash
# opshub-rkhunter.sh - weekly rootkit check (rkhunter) for the OpsHub Pi.
# Installed to /usr/local/sbin/opshub-rkhunter by security-automation-install.sh
# and run by opshub-rkhunter.timer (Wednesdays 02:40 AM, low priority).
#
# Results:
#   - No warnings -> writes /var/lib/opshub-security/rkhunter.ok
#   - Warnings    -> phone push (ntfy) with the first lines, full list in
#                    /var/log/opshub-rkhunter.log, stamp still written
#   - Failed run  -> no stamp, so the daily security check alerts
#
# Read-only: rkhunter only looks, it never changes system files.

STATE=/var/lib/opshub-security
LOG=/var/log/opshub-rkhunter.log
NTFY_URL_FILE=/home/frank/.opshub-ntfy-url

mkdir -p "$STATE"

notify() {  # title message priority tags
  [ -r "$NTFY_URL_FILE" ] || return 0
  url=$(tr -d '[:space:]' < "$NTFY_URL_FILE")
  [ -n "$url" ] || return 0
  curl -fsS -m 15 -H "Title: $1" -H "Priority: ${3:-default}" -H "Tags: ${4:-}" -d "$2" "$url" >/dev/null 2>&1 || true
}

exec 9>/run/opshub-rkhunter.lock
flock -n 9 || { echo "$(date -Iseconds) another check is running, skipping" >> "$LOG"; exit 0; }

echo "$(date -Iseconds) rkhunter started" >> "$LOG"

# Refresh its data files (needs internet; a failure here is not fatal).
rkhunter --update --nocolors >> "$LOG" 2>&1 || true

OUT=$(rkhunter --check --sk --nocolors --report-warnings-only 2>&1)
RC=$?

# 0 = clean, 1 = warnings. Anything else = the tool itself failed.
case "$RC" in
  0)
    echo "$(date -Iseconds) rkhunter finished: no warnings" >> "$LOG"
    touch "$STATE/rkhunter.ok"
    ;;
  1)
    echo "$(date -Iseconds) rkhunter finished: WARNINGS" >> "$LOG"
    echo "$OUT" >> "$LOG"
    touch "$STATE/rkhunter.ok"
    FIRST=$(echo "$OUT" | grep -i 'warning' | head -n 3 | tr '\n' ' ')
    notify "OpsHub: rootkit check has warnings" "${FIRST:-See /var/log/opshub-rkhunter.log}" high warning
    ;;
  *)
    echo "$(date -Iseconds) rkhunter FAILED (exit $RC)" >> "$LOG"
    echo "$OUT" | tail -n 20 >> "$LOG"
    exit "$RC"
    ;;
esac

tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
exit 0
