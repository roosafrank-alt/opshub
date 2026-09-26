#!/bin/bash
# opshub-uptime.sh - checks that OpsHub itself is actually responding (not
# just that the Pi is powered on) and pings Healthchecks.io, so Frank finds
# out OpsHub is down before someone on the floor can't scan a part.
#
# Install: use opshub-uptime-install.sh (run on your Mac) instead of doing
# this by hand. That script copies this file to the Pi, saves your ping
# URL, and wires up the cron line below for you. If you ever need to do it
# by hand:
#   mkdir -p ~/pi-scripts && cp opshub-uptime.sh ~/pi-scripts/
#   chmod +x ~/pi-scripts/opshub-uptime.sh
#   printf '%s' 'https://hc-ping.com/your-uuid-here' > ~/.opshub-uptime-url
#   chmod 600 ~/.opshub-uptime-url
#   crontab -e
#     */5 * * * * $HOME/pi-scripts/opshub-uptime.sh
#
# Undo:
#   crontab -e   # delete the line above
#   rm -f ~/pi-scripts/opshub-uptime.sh ~/.opshub-uptime-url ~/.opshub-uptime.log
#
# Healthchecks.io settings for this check: Period 5 minutes, Grace Time 10
# minutes (a single slow check doesn't alert, but two misses in a row - or
# the Pi being off/offline entirely, so no ping arrives at all - does).
#
# Runs from frank's own crontab, no sudo needed: it only reads OpsHub's
# login page locally over loopback and asks systemd for opshub's status,
# both of which any user can do.

URL_FILE="$HOME/.opshub-uptime-url"
LOG_FILE="$HOME/.opshub-uptime.log"
LOG_MAX_LINES=500
TIMEOUT=5
PORT=5050

trim_log() {
  [ -f "$LOG_FILE" ] || return 0
  tail -n "$LOG_MAX_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
}

log_down() {
  echo "$(date -Iseconds) DOWN - $1" >> "$LOG_FILE"
  trim_log
}

if [ ! -r "$URL_FILE" ]; then
  log_down "no ping URL at $URL_FILE - run opshub-uptime-install.sh first"
  exit 1
fi
PING_URL=$(tr -d '[:space:]' < "$URL_FILE")
if [ -z "$PING_URL" ]; then
  log_down "$URL_FILE is empty - run opshub-uptime-install.sh again"
  exit 1
fi

# The Pi runs OpsHub over HTTPS with a self-signed cert when one exists
# (so phone cameras can scan), plain HTTP otherwise - try HTTPS first,
# fall back to HTTP, since either counts as "up".
check_page() {
  curl -k -s -o /dev/null -w "%{http_code}" --max-time "$TIMEOUT" "$1" 2>/dev/null
}

https_code=$(check_page "https://127.0.0.1:$PORT/")
if [ "$https_code" = "200" ]; then
  page_ok="yes"
  page_detail="https 200"
else
  http_code=$(check_page "http://127.0.0.1:$PORT/")
  if [ "$http_code" = "200" ]; then
    page_ok="yes"
    page_detail="http 200 (https gave $https_code)"
  else
    page_ok="no"
    page_detail="https gave $https_code, http gave $http_code"
  fi
fi

if [ "$page_ok" = "yes" ]; then
  curl -fsS --max-time 10 "$PING_URL" -o /dev/null
  exit 0
fi

service_state=$(systemctl is-active opshub 2>/dev/null || echo "unknown")
disk_use=$(df -h / 2>/dev/null | awk 'NR==2 {print $5 " used on /"}')
reason="page check: $page_detail; opshub service: $service_state; disk: ${disk_use:-unknown}"

log_down "$reason"
curl -fsS --max-time 10 --data-raw "$reason" "$PING_URL/fail" -o /dev/null
