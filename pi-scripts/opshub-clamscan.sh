#!/bin/bash
# opshub-clamscan.sh - nightly ClamAV virus scan of the places malware would hide
# on the OpsHub Pi. Installed to /usr/local/sbin/opshub-clamscan by
# security-automation-install.sh and run by opshub-clamscan.timer (02:10 AM,
# low priority, so it never slows the app or the 3:00 AM backups).
#
# Results:
#   - Clean scan      -> writes /var/lib/opshub-security/clamscan.ok (the daily
#                        security check looks at that file's age).
#   - Something found -> phone push (ntfy) right away, details in
#                        /var/log/opshub-clamscan.log, and the stamp is still
#                        written (the scan itself did finish).
#   - Scan failed     -> no stamp, so the daily security check alerts after a day.
#
# It only READS files. It never deletes, moves or quarantines anything.

STATE=/var/lib/opshub-security
LOG=/var/log/opshub-clamscan.log
NTFY_URL_FILE=/home/frank/.opshub-ntfy-url

mkdir -p "$STATE"

notify() {  # title message priority tags
  [ -r "$NTFY_URL_FILE" ] || return 0
  url=$(tr -d '[:space:]' < "$NTFY_URL_FILE")
  [ -n "$url" ] || return 0
  curl -fsS -m 15 -H "Title: $1" -H "Priority: ${3:-default}" -H "Tags: ${4:-}" -d "$2" "$url" >/dev/null 2>&1 || true
}

# Only one scan at a time.
exec 9>/run/opshub-clamscan.lock
flock -n 9 || { echo "$(date -Iseconds) another scan is running, skipping" >> "$LOG"; exit 0; }

echo "$(date -Iseconds) scan started" >> "$LOG"

OUT=$(clamscan -r -i --no-summary \
  --exclude-dir='^/home/frank/shopinv-backups' \
  --exclude-dir='^/home/frank/\.cache' \
  --exclude-dir='^/home/frank/shopinv/vendor' \
  /home /etc /usr/local /opt /tmp /var/tmp 2>&1)
RC=$?

case "$RC" in
  0)
    echo "$(date -Iseconds) scan finished: clean" >> "$LOG"
    touch "$STATE/clamscan.ok"
    ;;
  1)
    echo "$(date -Iseconds) scan finished: INFECTED FILES FOUND" >> "$LOG"
    echo "$OUT" >> "$LOG"
    touch "$STATE/clamscan.ok"
    COUNT=$(echo "$OUT" | grep -c 'FOUND')
    notify "OpsHub: virus scan found something" "ClamAV flagged $COUNT file(s) on OpsHub. Nothing was deleted. Details: /var/log/opshub-clamscan.log" urgent rotating_light
    ;;
  *)
    echo "$(date -Iseconds) scan FAILED (exit $RC)" >> "$LOG"
    echo "$OUT" | tail -n 20 >> "$LOG"
    exit "$RC"
    ;;
esac

# Keep the log from growing forever.
tail -n 2000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
exit 0
