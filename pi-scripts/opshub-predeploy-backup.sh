#!/bin/bash
# opshub-predeploy-backup.sh - snapshot the live OpsHub app dir on the Pi
# right before a deploy overwrites it, so opshub-rollback.sh has something
# to restore if the new version crashes the app.
#
# Usage: ~/pi-scripts/opshub-predeploy-backup.sh [app-dir]
# Defaults to $OPSHUB_APP_DIR or ~/opshub if no argument is given.
#
# Called automatically as part of every "Deploy to Pi" rsync command (see
# withChecks() in the Idea Queue artifact) - runs on the Pi, right after
# predeploy_check.py passes and right before rsync overwrites the app dir.
# Exits 0 (and does nothing) if the app dir doesn't exist yet, so it's safe
# on a brand-new install with nothing to back up.
#
# Keeps the last 10 backups in ~/opshub-backups and prunes older ones, so
# this doesn't quietly fill the Pi's disk over time.

set -euo pipefail

APP_DIR="${1:-${OPSHUB_APP_DIR:-$HOME/opshub}}"
BACKUP_DIR="$HOME/opshub-backups"
KEEP=10

if [ ! -d "$APP_DIR" ]; then
  echo "opshub-predeploy-backup: no app dir at $APP_DIR yet - nothing to back up, continuing."
  exit 0
fi

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
ARCHIVE="$BACKUP_DIR/backup-$STAMP.tar.gz"

tar -czf "$ARCHIVE" -C "$(dirname "$APP_DIR")" "$(basename "$APP_DIR")"
echo "opshub-predeploy-backup: saved $ARCHIVE"

# Prune: keep only the $KEEP most recent backups (pre-rollback-* copies made
# by opshub-rollback.sh are pruned the same way, counted separately).
ls -t "$BACKUP_DIR"/backup-*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
ls -t "$BACKUP_DIR"/pre-rollback-*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f
