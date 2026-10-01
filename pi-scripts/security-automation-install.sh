#!/bin/bash
# security-automation-install.sh - one-time installer for the OpsHub Pi's
# automatic security checks (the same idea as forge's). Run it ON THE PI:
#
#   cd ~/opshub-security/pi-scripts && sudo ./security-automation-install.sh
#
# What it does:
#   1. installs official Debian packages: clamav, clamav-freshclam, rkhunter,
#      unattended-upgrades (no mail server, no extras)
#   2. copies the three scripts next to this installer into /usr/local/sbin:
#        opshub-clamscan        nightly virus scan, 02:10 AM
#        opshub-rkhunter        weekly rootkit check, Wednesdays 02:40 AM
#        opshub-security-check  daily "are those still happening?" check, 06:40 AM
#   3. turns on automatic SECURITY updates only (Debian security repository).
#      It never reboots by itself; the Sunday 3:30 AM restart picks up kernels.
#   4. stops rkhunter's own daily Debian job (ours replaces it) and lets it
#      refresh its file list after apt updates, so updates don't cause false alarms
#   5. downloads the latest virus signatures once and keeps them updating
#   6. asks for the Healthchecks.io ping address of the "OpsHub security" check
#      (Period 1 day, Grace Time 3 hours) and saves it root-only in
#      /etc/opshub-security/hc-url
#   7. adds the three timers and starts them
#
# It does NOT touch Tailscale, Funnel, the firewall, SSH, fail2ban, the app,
# its database, cron, or any backup. Safe to run twice.
#
# Undo:
#   sudo systemctl disable --now opshub-clamscan.timer opshub-rkhunter.timer opshub-security-check.timer
#   sudo rm -f /etc/systemd/system/opshub-{clamscan,rkhunter,security-check}.{service,timer} \
#              /usr/local/sbin/opshub-{clamscan,rkhunter,security-check}
#   sudo systemctl daemon-reload
#   (packages stay; remove with: sudo apt remove clamav rkhunter unattended-upgrades)

set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo:  sudo ./security-automation-install.sh" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE=/var/lib/opshub-security
CONF=/etc/opshub-security

for f in opshub-clamscan.sh opshub-rkhunter.sh opshub-security-check.sh; do
  [ -f "$SRC_DIR/$f" ] || { echo "Missing $f next to this installer. Run it from inside pi-scripts/." >&2; exit 1; }
done

echo "Step 1 of 7 - packages (official Debian repository)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  clamav clamav-freshclam rkhunter unattended-upgrades
echo "  done"

echo "Step 2 of 7 - installing the scripts"
install -m 755 "$SRC_DIR/opshub-clamscan.sh"        /usr/local/sbin/opshub-clamscan
install -m 755 "$SRC_DIR/opshub-rkhunter.sh"        /usr/local/sbin/opshub-rkhunter
install -m 755 "$SRC_DIR/opshub-security-check.sh"  /usr/local/sbin/opshub-security-check
mkdir -p "$STATE" "$CONF"
[ -e "$STATE/installed" ] || touch "$STATE/installed"
echo "  done"

echo "Step 3 of 7 - automatic security updates (no automatic reboot)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
echo "  done"

echo "Step 4 of 7 - rkhunter settings"
if [ -f /etc/default/rkhunter ]; then
  sed -i 's/^#\?CRON_DAILY_RUN=.*/CRON_DAILY_RUN="false"/'   /etc/default/rkhunter
  sed -i 's/^#\?CRON_DB_UPDATE=.*/CRON_DB_UPDATE="false"/'   /etc/default/rkhunter
  sed -i 's/^#\?APT_AUTOGEN=.*/APT_AUTOGEN="true"/'          /etc/default/rkhunter
fi
rkhunter --propupd >/dev/null 2>&1 || echo "  (rkhunter baseline will be set on its first run)"
echo "  done"

echo "Step 5 of 7 - virus signatures (this can take a minute or two)"
systemctl stop clamav-freshclam 2>/dev/null || true
freshclam || echo "  (freshclam could not download now; the background updater will retry)"
systemctl enable --now clamav-freshclam >/dev/null 2>&1 || true
echo "  done"

echo "Step 6 of 7 - Healthchecks.io ping address for 'OpsHub security'"
if [ -s "$CONF/hc-url" ]; then
  echo "  an address is already saved; press Enter to keep it, or paste a new one:"
else
  echo "  paste the check's address (https://hc-ping.com/...), or press Enter to skip for now:"
fi
read -r PING_URL </dev/tty || PING_URL=""
if [ -n "$PING_URL" ]; then
  case "$PING_URL" in
    https://hc-ping.com/*) ;;
    *) echo "That doesn't start with https://hc-ping.com/ - nothing saved." >&2; PING_URL="" ;;
  esac
fi
if [ -n "$PING_URL" ]; then
  umask 077
  printf '%s' "$PING_URL" > "$CONF/hc-url"
  chmod 600 "$CONF/hc-url"
  echo "  saved (root only)"
else
  echo "  kept as is / skipped"
fi

echo "Step 7 of 7 - timers"
cat > /etc/systemd/system/opshub-clamscan.service <<'UNIT'
[Unit]
Description=OpsHub nightly ClamAV scan
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/opshub-clamscan
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=3h
UNIT
cat > /etc/systemd/system/opshub-clamscan.timer <<'UNIT'
[Unit]
Description=OpsHub nightly ClamAV scan at 2:10 AM

[Timer]
OnCalendar=*-*-* 02:10:00

[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/opshub-rkhunter.service <<'UNIT'
[Unit]
Description=OpsHub weekly rootkit check
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/opshub-rkhunter
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=1h
UNIT
cat > /etc/systemd/system/opshub-rkhunter.timer <<'UNIT'
[Unit]
Description=OpsHub weekly rootkit check, Wednesdays 2:40 AM

[Timer]
OnCalendar=Wed *-*-* 02:40:00

[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/opshub-security-check.service <<'UNIT'
[Unit]
Description=OpsHub daily security freshness check
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/opshub-security-check
UNIT
cat > /etc/systemd/system/opshub-security-check.timer <<'UNIT'
[Unit]
Description=OpsHub daily security freshness check at 6:40 AM

[Timer]
OnCalendar=*-*-* 06:40:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT
chmod 644 /etc/systemd/system/opshub-{clamscan,rkhunter,security-check}.{service,timer}
systemctl daemon-reload
systemctl enable --now opshub-clamscan.timer opshub-rkhunter.timer opshub-security-check.timer >/dev/null 2>&1
echo "  done"

echo
echo "Timers:"
systemctl list-timers --no-pager | grep -E "^NEXT|opshub-(clamscan|rkhunter|security)" || true
echo
echo "Running the daily check once now (this also sends the first Healthchecks.io ping):"
if /usr/local/sbin/opshub-security-check; then
  echo "SUCCESS - the check passed."
else
  echo "The check reported a problem - see: sudo tail -n 5 /var/log/opshub-security.log"
fi
echo
echo "Optional: start the first virus scan now instead of waiting for 2:10 AM:"
echo "  sudo systemctl start opshub-clamscan.service   (runs in the background; log: /var/log/opshub-clamscan.log)"
