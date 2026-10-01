#!/bin/bash
# lynis-install.sh - adds the weekly Lynis security audit to the OpsHub Pi.
# Run it ON THE PI after security-automation-install.sh:
#
#   cd ~/opshub-security/pi-scripts && sudo ./lynis-install.sh
#
# Installs the official Debian package "lynis", the script opshub-lynis, a timer
# (Saturdays 04:10 AM), and the updated daily check that also watches the audit.
# Touches nothing else. Safe to run twice.
#
# Undo:
#   sudo systemctl disable --now opshub-lynis.timer
#   sudo rm -f /etc/systemd/system/opshub-lynis.{service,timer} /usr/local/sbin/opshub-lynis
#   sudo systemctl daemon-reload   (then re-run security-automation-install.sh's check from git history if needed)

set -e
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo:  sudo ./lynis-install.sh" >&2; exit 1; }
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for f in opshub-lynis.sh opshub-security-check.sh; do
  [ -f "$SRC_DIR/$f" ] || { echo "Missing $f next to this installer." >&2; exit 1; }
done

echo "Step 1 of 3 - installing Lynis (official Debian package)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends lynis
echo "  done"

echo "Step 2 of 3 - scripts"
install -m 755 "$SRC_DIR/opshub-lynis.sh"          /usr/local/sbin/opshub-lynis
install -m 755 "$SRC_DIR/opshub-security-check.sh" /usr/local/sbin/opshub-security-check
mkdir -p /var/lib/opshub-security
[ -e /var/lib/opshub-security/lynis.installed ] || touch /var/lib/opshub-security/lynis.installed
echo "  done"

echo "Step 3 of 3 - weekly timer"
cat > /etc/systemd/system/opshub-lynis.service <<'UNIT'
[Unit]
Description=OpsHub weekly Lynis security audit
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/opshub-lynis
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=1h
UNIT
cat > /etc/systemd/system/opshub-lynis.timer <<'UNIT'
[Unit]
Description=OpsHub weekly Lynis audit, Saturdays 4:10 AM

[Timer]
OnCalendar=Sat *-*-* 04:10:00

[Install]
WantedBy=timers.target
UNIT
chmod 644 /etc/systemd/system/opshub-lynis.{service,timer}
systemctl daemon-reload
systemctl enable --now opshub-lynis.timer >/dev/null 2>&1
echo "  done"
echo
echo "Optional: run the first audit now (about a minute):"
echo "  sudo systemctl start opshub-lynis.service; sudo cat /var/lib/opshub-security/lynis.score"
