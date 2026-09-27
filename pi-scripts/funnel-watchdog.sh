#!/bin/bash
# funnel-watchdog.sh - keeps OpsHub's public Tailscale Funnel address
# (https://opshub.taila1bcc5.ts.net) switched ON, and turns it back on if
# anything switches it off. Runs as root via cron every 5 minutes and at boot.
#
# Why: phones reach OpsHub over Funnel, with no Wi-Fi or Tailscale app needed.
# On Sep 27, 2026 Funnel got switched back to "tailnet only" (a plain
# `tailscale serve ...` command replaces the Funnel setting), and every
# phone got "cannot establish a secure connection" even though the app
# itself was running fine. This puts it back within 5 minutes, whatever
# turned it off.
#
# Install (on the Pi):
#   sudo cp funnel-watchdog.sh /usr/local/sbin/funnel-watchdog.sh
#   sudo chmod +x /usr/local/sbin/funnel-watchdog.sh
#   sudo crontab -e
#     */5 * * * * /usr/local/sbin/funnel-watchdog.sh >> /var/log/funnel-watchdog.log 2>&1
#     @reboot sleep 60 && /usr/local/sbin/funnel-watchdog.sh >> /var/log/funnel-watchdog.log 2>&1
#
# Undo:
#   sudo crontab -e   # delete the two lines above
#   sudo rm -f /usr/local/sbin/funnel-watchdog.sh /var/log/funnel-watchdog.log

TARGET="https+insecure://127.0.0.1:5050"
LOG_TAG="funnel-watchdog"

log() {
  echo "$(date -Iseconds) $1"
  logger -t "$LOG_TAG" "$1" 2>/dev/null
}

if ! command -v tailscale >/dev/null 2>&1; then
  log "tailscale not installed - nothing to do"
  exit 1
fi

status=$(tailscale funnel status 2>&1)

# Healthy = Funnel on AND still pointing at OpsHub. Quiet on success so the
# log only has the times it had to step in.
if echo "$status" | grep -q "(Funnel on)" && echo "$status" | grep -qF "$TARGET"; then
  exit 0
fi

log "Funnel was not on for OpsHub - turning it back on. Status was: $(echo "$status" | tr '\n' ' ')"
if tailscale funnel --bg "$TARGET" >/dev/null 2>&1; then
  log "Funnel back on: $(tailscale funnel status 2>&1 | grep -m1 -F '(Funnel on)')"
else
  log "FAILED to turn Funnel back on - check 'sudo tailscale funnel status' and the Tailscale admin console (funnel attribute)"
  exit 1
fi
