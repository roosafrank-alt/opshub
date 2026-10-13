#!/bin/bash
# resource-scan.sh - the twice-a-month check that Flight Academy Resources still
# match the official FAA / eCFR versions. Runs resource_scan.py from the live app
# folder: it looks at each resource's Source link (faa.gov, ecfr.gov, govinfo.gov
# only), files a pending update when something newer is out, and sends the
# admins one phone alert. It never replaces a resource: an admin approves each
# one on the Resources page. The "Check now" button on that page runs the same job.
#
# Install (once, by hand - the Pi's puller does not change crontabs):
#   mkdir -p ~/pi-scripts && cp resource-scan.sh ~/pi-scripts/
#   chmod +x ~/pi-scripts/resource-scan.sh
#   crontab -e
#     0 4 1,15 * * $HOME/pi-scripts/resource-scan.sh
#
# Undo:
#   crontab -e   # delete the line above
#   rm -f ~/pi-scripts/resource-scan.sh ~/.opshub-resource-scan.log
#
# Log: ~/.opshub-resource-scan.log

APP_DIR="$HOME/shopinv"
LOG="$HOME/.opshub-resource-scan.log"

cd "$APP_DIR" || exit 1
PYTHONPATH="$APP_DIR/vendor" python3 resource_scan.py >> "$LOG" 2>&1
# keep the log small
tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
