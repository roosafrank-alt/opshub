#!/bin/bash
# opshub-uptime-install.sh - one-time installer. Run this on YOUR MAC, not
# the Pi (it needs opshub-uptime.sh sitting right next to it in this
# pi-scripts/ folder). It:
#   1. asks you to paste your Healthchecks.io ping URL
#   2. copies opshub-uptime.sh to the Pi
#   3. saves the ping URL on the Pi at ~/.opshub-uptime-url, readable only
#      by frank (this URL is private - it never goes in this repo)
#   4. adds the cron line (every 5 minutes) without duplicating it if you
#      run this installer again
#   5. runs the check once immediately and tells you SUCCESS or why not
#
# Usage:
#   cd pi-scripts && ./opshub-uptime-install.sh
#
# Undo: ssh frank@100.101.116.22, then
#   crontab -e   # delete the opshub-uptime.sh line
#   rm -f ~/pi-scripts/opshub-uptime.sh ~/.opshub-uptime-url ~/.opshub-uptime.log
#
# Healthchecks.io settings to create the check with: Period 5 minutes,
# Grace Time 10 minutes.

set -e

PI_HOST="frank@100.101.116.22"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="opshub-uptime.sh"
REMOTE_DIR="pi-scripts"
REMOTE_SCRIPT="\$HOME/$REMOTE_DIR/$SCRIPT_NAME"
CRON_LINE="*/5 * * * * $REMOTE_SCRIPT"

if [ ! -f "$SCRIPT_DIR/$SCRIPT_NAME" ]; then
  echo "Can't find $SCRIPT_NAME next to this installer - run it from inside pi-scripts/." >&2
  exit 1
fi

echo "Paste your Healthchecks.io ping URL (looks like https://hc-ping.com/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx):"
read -r PING_URL
if [ -z "$PING_URL" ]; then
  echo "No URL entered - aborting, nothing was changed." >&2
  exit 1
fi

echo "Copying $SCRIPT_NAME to $PI_HOST:$REMOTE_DIR/ ..."
ssh "$PI_HOST" "mkdir -p $REMOTE_DIR"
scp "$SCRIPT_DIR/$SCRIPT_NAME" "$PI_HOST:$REMOTE_DIR/$SCRIPT_NAME"
ssh "$PI_HOST" "chmod +x $REMOTE_DIR/$SCRIPT_NAME"

echo "Saving your ping URL on the Pi (chmod 600 - only you can read it) ..."
printf '%s' "$PING_URL" | ssh "$PI_HOST" "cat > .opshub-uptime-url && chmod 600 .opshub-uptime-url"

echo "Adding the cron line (every 5 minutes, skipping if it's already there) ..."
ssh "$PI_HOST" "(crontab -l 2>/dev/null | grep -vF '$SCRIPT_NAME'; echo '$CRON_LINE') | crontab -"

echo "Running the check once ..."
set +e
OUTPUT=$(ssh "$PI_HOST" "bash $REMOTE_DIR/$SCRIPT_NAME" 2>&1)
EXIT_CODE=$?
set -e

if [ "$EXIT_CODE" = "0" ]; then
  echo "SUCCESS - OpsHub answered locally on the Pi and Healthchecks.io was pinged."
else
  echo "Check did NOT succeed:"
  REASON=$(ssh "$PI_HOST" "tail -n 1 .opshub-uptime.log" 2>/dev/null)
  echo "  ${REASON:-$OUTPUT}"
  echo "(full history in ~/.opshub-uptime.log on the Pi)"
fi

echo
echo "Healthchecks.io settings for this check: Period = 5 minutes, Grace Time = 10 minutes."
