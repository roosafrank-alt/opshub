#!/bin/bash
# backup-watchdog.sh - confirms, every morning, that last night's OpsHub
# database backup really landed in all three places, and alerts Frank if any
# one of them didn't:
#
#   1. On the Pi itself     ~/shopinv-backups        (made by shopinv-backup.sh, 3:00 AM)
#   2. On the USB drive     /mnt/backupdrive         (3:00 AM script + opshub-usb-backup.sh, 3:15 AM)
#   3. In Backblaze B2      the rclone path in ~/.backup-watchdog.conf (3:00 AM script)
#
# "Landed" means: a backup file newer than MAX_AGE_HOURS exists there, it isn't
# empty, and (for the Pi and USB copies) it passes a quick corruption check.
#
# How Frank gets told:
#   - Something missing/old/broken -> an ntfy push to his phone right away
#     (same ~/.opshub-ntfy-url topic pi_health.py uses) AND a /fail ping to
#     Healthchecks.io, which emails/pushes too.
#   - All three fine -> a success ping to Healthchecks.io.
#   - This script never runs at all (Pi off, cron broken, script deleted) ->
#     Healthchecks.io gets no ping and alerts on its own after the grace time.
#     That's the part a script can't do for itself.
#
# Healthchecks.io settings for this check: Period 1 day, Grace Time 3 hours.
#
# Install: run backup-watchdog-install.sh on your Mac (it does all of this).
# By hand, on the Pi:
#   mkdir -p ~/pi-scripts && cp backup-watchdog.sh ~/pi-scripts/ && chmod +x ~/pi-scripts/backup-watchdog.sh
#   create ~/.backup-watchdog.conf (chmod 600) with:
#     HC_URL=https://hc-ping.com/your-uuid-here
#     B2_PATH=yourremote:your-bucket/folder
#   crontab -e
#     30 6 * * * $HOME/pi-scripts/backup-watchdog.sh
#
# Undo:
#   crontab -e   # delete the line above
#   rm -f ~/pi-scripts/backup-watchdog.sh ~/.backup-watchdog.conf ~/.backup-watchdog.log
#
# Read-only: it never writes to, moves or deletes any backup. Runs from
# frank's own crontab, no sudo.

CONF_FILE="$HOME/.backup-watchdog.conf"
NTFY_URL_FILE="$HOME/.opshub-ntfy-url"
LOG_FILE="$HOME/.backup-watchdog.log"
LOG_MAX_LINES=400

PI_DIR="$HOME/shopinv-backups"
USB_MOUNT="/mnt/backupdrive"
MAX_AGE_HOURS=26          # nightly at 3 AM, checked 6:30 AM; 26h allows a late run
MIN_BYTES=10240           # a real OpsHub database is far bigger than 10 KB

HC_URL=""
B2_PATH=""
[ -r "$CONF_FILE" ] && . "$CONF_FILE"

MAX_AGE_MIN=$((MAX_AGE_HOURS * 60))
problems=()
confirmed=()

log() {
  echo "$(date -Iseconds) $1" >> "$LOG_FILE"
  tail -n "$LOG_MAX_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
}

join() {  # join args with "; "
  local out="" x
  for x in "$@"; do out="${out:+$out; }$x"; done
  echo "$out"
}

human_age() {  # seconds -> "3h 12m"
  local s=$1
  echo "$((s / 3600))h $(((s % 3600) / 60))m"
}

# Newest database-backup file under a folder (any depth), as "epoch size path".
# Only names that look like an OpsHub database backup count (shopinv*, *.db,
# *.db.gz, *.sqlite*), so pre-deploy code snapshots in the same folder can't
# stand in for a missing database backup.
newest_file() {
  find "$1" -type f \( -iname '*shopinv*' -o -iname '*.db' -o -iname '*.db.gz' -o -iname '*.sqlite*' \) \
    -printf '%T@ %s %p\n' 2>/dev/null | sort -n | tail -n 1
}

# Quick corruption check. .db / .sqlite -> SQLite quick_check (read-only
# open, so it can't touch the file). .gz -> gzip -t. Anything else: skip.
verify_file() {
  local f=$1
  case "$f" in
    *.gz)
      gzip -t "$f" 2>/dev/null || { echo "gzip test failed"; return 1; } ;;
    *.db|*.sqlite|*.sqlite3)
      local r
      r=$(python3 - "$f" <<'PY' 2>&1
import sqlite3, sys
con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print(con.execute("PRAGMA quick_check").fetchone()[0])
PY
)
      [ "$r" = "ok" ] || { echo "SQLite check said: $(echo "$r" | tail -n 1 | cut -c1-120)"; return 1; } ;;
  esac
  return 0
}

check_folder() {  # label, folder
  local label=$1 dir=$2 line epoch size path age why
  if [ ! -d "$dir" ]; then
    problems+=("$label: folder $dir is missing")
    return
  fi
  line=$(newest_file "$dir")
  if [ -z "$line" ]; then
    problems+=("$label: no database backup files at all in $dir")
    return
  fi
  epoch=${line%%.*}
  size=$(echo "$line" | awk '{print $2}')
  path=$(echo "$line" | cut -d' ' -f3-)
  age=$(( $(date +%s) - epoch ))
  if [ "$age" -gt $((MAX_AGE_MIN * 60)) ]; then
    problems+=("$label: newest backup is $(human_age "$age") old ($(basename "$path"))")
    return
  fi
  if [ "$size" -lt "$MIN_BYTES" ]; then
    problems+=("$label: newest backup is only $size bytes ($(basename "$path"))")
    return
  fi
  if ! why=$(verify_file "$path"); then
    problems+=("$label: newest backup $(basename "$path") looks damaged - $why")
    return
  fi
  confirmed+=("$label OK ($(basename "$path"), $(human_age "$age") old)")
}

# 1. Pi
check_folder "Pi copy" "$PI_DIR"

# 2. USB drive - must actually be mounted, or we'd be looking at the empty
#    folder underneath and could miss that backups have been going nowhere.
if [ "${SKIP_MOUNT_CHECK:-0}" != "1" ] && ! mountpoint -q "$USB_MOUNT" 2>/dev/null; then
  problems+=("USB drive: not mounted at $USB_MOUNT (unplugged or failed?)")
else
  check_folder "USB drive" "$USB_MOUNT"
fi

# 3. Backblaze - list only files changed in the last MAX_AGE_HOURS.
if [ -z "$B2_PATH" ]; then
  problems+=("Backblaze: B2_PATH not set in $CONF_FILE - can't check it")
elif ! command -v rclone >/dev/null 2>&1; then
  problems+=("Backblaze: rclone isn't installed on the Pi")
else
  b2_out=$(timeout 120 rclone lsl --max-age "${MAX_AGE_HOURS}h" "$B2_PATH" 2>&1)
  b2_rc=$?
  if [ "$b2_rc" -ne 0 ]; then
    problems+=("Backblaze: couldn't list $B2_PATH (rclone exit $b2_rc: $(echo "$b2_out" | tail -n 1 | cut -c1-120))")
  else
    b2_best=$(echo "$b2_out" | awk 'NF>=4 {print $1, $NF}' \
      | grep -iE '(shopinv|\.db$|\.db\.gz$|\.sqlite)' | sort -n | tail -n 1)
    b2_size=${b2_best%% *}
    if [ -z "$b2_best" ]; then
      problems+=("Backblaze: nothing uploaded in the last ${MAX_AGE_HOURS}h")
    elif [ "$b2_size" -lt "$MIN_BYTES" ]; then
      problems+=("Backblaze: newest upload is only $b2_size bytes")
    else
      confirmed+=("Backblaze OK ($(basename "${b2_best#* }"))")
    fi
  fi
fi

# ---- report ----
if [ ${#problems[@]} -eq 0 ]; then
  msg="All 3 OpsHub backups confirmed: $(join "${confirmed[@]}")"
  log "OK - $msg"
  [ -n "$HC_URL" ] && curl -fsS --max-time 15 --retry 3 --data-raw "$msg" "$HC_URL" -o /dev/null
  exit 0
fi

msg="OpsHub BACKUP PROBLEM ($(date '+%a %b %-d')): $(join "${problems[@]}")"
[ ${#confirmed[@]} -gt 0 ] && msg="$msg. Still fine: $(join "${confirmed[@]}")"
msg="$msg. Details: ~/shopinv-backup.log and ~/.backup-watchdog.log on the Pi."
log "FAIL - $msg"

if [ -r "$NTFY_URL_FILE" ]; then
  ntfy=$(tr -d '[:space:]' < "$NTFY_URL_FILE")
  [ -n "$ntfy" ] && curl -fsS --max-time 15 --retry 3 \
    -H "Title: OpsHub backup missed" -H "Priority: high" -H "Tags: warning" \
    --data-raw "$msg" "$ntfy" -o /dev/null
fi
[ -n "$HC_URL" ] && curl -fsS --max-time 15 --retry 3 --data-raw "$msg" "$HC_URL/fail" -o /dev/null
exit 1
