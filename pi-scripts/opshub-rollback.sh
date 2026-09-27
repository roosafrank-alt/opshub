#!/bin/bash
# opshub-rollback.sh - restore the previous OpsHub release on the Pi and
# restart the service, in case a deploy crashes the app or breaks a page.
#
# Pairs with the backup step that tools/predeploy_check.py's deploy wrapper
# now takes before every rsync (see withChecks() in the Idea Queue artifact
# and the "Deploy backup" section of tools/predeploy_check.py): each deploy
# tars up the current live app dir to ~/opshub-backups/<timestamp>.tar.gz
# on the Pi before the new version is rsynced over it. This script restores
# the newest one of those.
#
# Usage (run ON THE PI):
#   ~/pi-scripts/opshub-rollback.sh [path-to-backup.tar.gz]
#
# With no argument, restores the most recent backup in ~/opshub-backups.
# Pass a specific archive path to restore an older one instead (see
# `ls -lt ~/opshub-backups` for the list).
#
# What it does:
#   1. Finds the backup archive (newest, or the one you named).
#   2. Stops the opshub service.
#   3. Tars up the CURRENT (about-to-be-replaced) app dir first, as a
#      "pre-rollback" safety copy, so rolling back is itself undoable.
#   4. Extracts the chosen backup over the app dir.
#   5. Restarts the opshub service and checks it came back up.
#
# Safe to run more than once - it always backs up what's live before
# touching anything.
#
# Undo: if a rollback itself goes wrong, the "pre-rollback" archive it just
# made is in ~/opshub-backups too (name starts with pre-rollback-) - restore
# that the same way: ~/pi-scripts/opshub-rollback.sh ~/opshub-backups/pre-rollback-*.tar.gz

set -euo pipefail

BACKUP_DIR="$HOME/opshub-backups"
APP_DIR="${OPSHUB_APP_DIR:-$HOME/opshub}"
SERVICE="opshub"
PORT=5050
LOG_FILE="$HOME/.opshub-rollback.log"

log() { echo "$(date -Iseconds) $1" | tee -a "$LOG_FILE"; }

if [ ! -d "$BACKUP_DIR" ]; then
  log "FAILED: no backup directory at $BACKUP_DIR - nothing to roll back to (has a deploy run since this script was installed?)"
  exit 1
fi

if [ $# -ge 1 ]; then
  ARCHIVE="$1"
else
  ARCHIVE=$(ls -t "$BACKUP_DIR"/backup-*.tar.gz 2>/dev/null | head -n1 || true)
fi

if [ -z "${ARCHIVE:-}" ] || [ ! -f "$ARCHIVE" ]; then
  log "FAILED: no backup archive found (looked for $BACKUP_DIR/backup-*.tar.gz, or the path you passed: ${1:-<none>})"
  exit 1
fi

log "Rolling back to $ARCHIVE"

if [ ! -d "$APP_DIR" ]; then
  log "FAILED: app dir $APP_DIR doesn't exist - check OPSHUB_APP_DIR"
  exit 1
fi

log "Stopping $SERVICE"
sudo systemctl stop "$SERVICE" 2>/dev/null || systemctl --user stop "$SERVICE" 2>/dev/null || {
  log "Couldn't stop the service with systemctl (tried sudo and --user) - continuing anyway, but check manually if this looks wrong."
}

PRE_ROLLBACK="$BACKUP_DIR/pre-rollback-$(date +%Y%m%d-%H%M%S).tar.gz"
log "Backing up current app dir first, to $PRE_ROLLBACK"
tar -czf "$PRE_ROLLBACK" -C "$(dirname "$APP_DIR")" "$(basename "$APP_DIR")"

log "Extracting $ARCHIVE over $APP_DIR"
TMP_RESTORE=$(mktemp -d)
tar -xzf "$ARCHIVE" -C "$TMP_RESTORE"
RESTORED_NAME=$(basename "$APP_DIR")
if [ ! -d "$TMP_RESTORE/$RESTORED_NAME" ]; then
  # Backup archive may have been made with a different top-level dir name;
  # fall back to whatever single directory it contains.
  RESTORED_NAME=$(ls "$TMP_RESTORE" | head -n1)
fi
rsync -a --delete "$TMP_RESTORE/$RESTORED_NAME/" "$APP_DIR/"
rm -rf "$TMP_RESTORE"

log "Starting $SERVICE"
sudo systemctl start "$SERVICE" 2>/dev/null || systemctl --user start "$SERVICE" 2>/dev/null || {
  log "FAILED: couldn't start $SERVICE with systemctl (tried sudo and --user). Restore finished but the service needs to be started by hand."
  exit 1
}

sleep 2
if curl -k -s -o /dev/null -w "%{http_code}" --max-time 5 "https://127.0.0.1:$PORT/" 2>/dev/null | grep -q "^200$" \
  || curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$PORT/" 2>/dev/null | grep -q "^200$"; then
  log "OK - rolled back to $ARCHIVE and $SERVICE is responding on port $PORT"
  exit 0
else
  log "WARNING - rolled back to $ARCHIVE and restarted $SERVICE, but it isn't answering on port $PORT yet. Check: systemctl status $SERVICE"
  exit 2
fi
