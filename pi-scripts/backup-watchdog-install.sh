#!/bin/bash
# backup-watchdog-install.sh - one-time installer for backup-watchdog.sh.
# Run this on YOUR MAC, from inside the pi-scripts/ folder. It:
#   1. asks for the Healthchecks.io ping URL of a new "OpsHub backups" check
#   2. finds the Backblaze path your 3:00 AM backup uploads to (from
#      ~/shopinv-backup.sh on the Pi) and asks you to confirm it
#   3. copies backup-watchdog.sh to the Pi and saves both settings in
#      ~/.backup-watchdog.conf on the Pi (readable only by frank - never in this repo)
#   4. adds the cron line (6:30 AM daily) without duplicating it
#   5. runs the check once right now and shows you the result
#
# Usage:   cd pi-scripts && ./backup-watchdog-install.sh
#
# Undo: ssh frank@100.101.116.22, then
#   crontab -e   # delete the backup-watchdog.sh line
#   rm -f ~/pi-scripts/backup-watchdog.sh ~/.backup-watchdog.conf ~/.backup-watchdog.log
#
# Create the Healthchecks.io check with: Period = 1 day, Grace Time = 3 hours.

set -e

PI_HOST="frank@100.101.116.22"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="backup-watchdog.sh"
REMOTE_DIR="pi-scripts"
CRON_LINE="30 6 * * * \$HOME/$REMOTE_DIR/$SCRIPT_NAME"

if [ ! -f "$SCRIPT_DIR/$SCRIPT_NAME" ]; then
  echo "Can't find $SCRIPT_NAME next to this installer - run it from inside pi-scripts/." >&2
  exit 1
fi

echo "Step 1 of 5 - Healthchecks.io"
echo "In Healthchecks.io, add a check named 'OpsHub backups' (Period 1 day, Grace 3 hours),"
echo "then paste its ping URL here (https://hc-ping.com/...):"
read -r HC_URL
if [ -z "$HC_URL" ]; then
  echo "No URL entered - stopping, nothing was changed." >&2
  exit 1
fi

echo
echo "Step 2 of 5 - finding the Backblaze path in ~/shopinv-backup.sh on the Pi ..."
RCLONE_LINES=$(ssh "$PI_HOST" "grep -n 'rclone' ~/shopinv-backup.sh 2>/dev/null" || true)
GUESS=$(echo "$RCLONE_LINES" | grep -oE '[A-Za-z0-9_-]+:[A-Za-z0-9_./${}-]*' | grep -v '^http' | tail -n 1)
if [ -n "$RCLONE_LINES" ]; then
  echo "These are the rclone lines in the backup script:"
  echo "$RCLONE_LINES" | sed 's/^/    /'
fi
if [ -n "$GUESS" ] && ! echo "$GUESS" | grep -q '\$'; then
  echo "Best guess for the Backblaze folder: $GUESS"
  echo "Press Return to use it, or type the correct one (like  b2:my-bucket/opshub ):"
else
  echo "Couldn't work out the exact folder (it may be built from a variable)."
  echo "Type the Backblaze folder the backup uploads to (like  b2:my-bucket/opshub ):"
fi
read -r B2_PATH
B2_PATH=${B2_PATH:-$GUESS}
if [ -z "$B2_PATH" ] || echo "$B2_PATH" | grep -q '\$'; then
  echo "No usable Backblaze path - stopping, nothing was changed." >&2
  exit 1
fi

echo
echo "Step 3 of 5 - copying the watchdog to the Pi and saving settings ..."
ssh "$PI_HOST" "mkdir -p $REMOTE_DIR"
scp -q "$SCRIPT_DIR/$SCRIPT_NAME" "$PI_HOST:$REMOTE_DIR/$SCRIPT_NAME"
ssh "$PI_HOST" "chmod +x $REMOTE_DIR/$SCRIPT_NAME"
printf 'HC_URL=%q\nB2_PATH=%q\n' "$HC_URL" "$B2_PATH" \
  | ssh "$PI_HOST" "umask 077 && cat > .backup-watchdog.conf && chmod 600 .backup-watchdog.conf"
if ! ssh "$PI_HOST" "test -s .opshub-ntfy-url"; then
  echo "  Note: no ~/.opshub-ntfy-url on the Pi, so there'll be no instant ntfy push -"
  echo "  alerts will come from Healthchecks.io only."
fi

echo "Step 4 of 5 - adding the 6:30 AM daily cron line ..."
ssh "$PI_HOST" "(crontab -l 2>/dev/null | grep -vF '$SCRIPT_NAME'; echo '$CRON_LINE') | crontab -"

echo "Step 5 of 5 - running the check once now ..."
set +e
ssh "$PI_HOST" "bash $REMOTE_DIR/$SCRIPT_NAME" >/dev/null 2>&1
EXIT_CODE=$?
RESULT=$(ssh "$PI_HOST" "tail -n 1 .backup-watchdog.log" 2>/dev/null)
set -e

echo
if [ "$EXIT_CODE" = "0" ]; then
  echo "SUCCESS - all three backups (Pi, USB drive, Backblaze) were confirmed:"
else
  echo "The check found a problem (you should also have just got an alert):"
fi
echo "  $RESULT"
echo
echo "From now on it runs every morning at 6:30. Log: ~/.backup-watchdog.log on the Pi."
