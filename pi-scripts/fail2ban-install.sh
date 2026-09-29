#!/bin/bash
# fail2ban-install.sh - one-time installer that bans anyone who keeps guessing
# SSH passwords on the Pi. Run it ON THE PI (ssh in first), from ~/shopinv/pi-scripts:
#
#   cd ~/shopinv/pi-scripts && sudo ./fail2ban-install.sh
#
# What it does:
#   1. installs the fail2ban apt package (if it isn't already there)
#   2. writes /etc/fail2ban/jail.d/opshub-sshd.local: ONLY the sshd jail is on
#      (5 failed tries in 10 minutes -> banned for 1 hour)
#   3. never bans localhost, Tailscale (100.64.0.0/10) or the shop's local
#      network (192.168.0.0/16, 10.0.0.0/8), so Frank, Claude's tasks and the
#      watchdogs can't be locked out
#   4. restarts fail2ban and shows 'fail2ban-client status sshd'
#   5. prints (does not change) whether SSH password login is currently on
#
# Safe to run twice: it just rewrites the same file. There is deliberately NO
# jail for the web app / Funnel (all web traffic arrives from the local proxy,
# so it would ban everyone). It doesn't touch sshd_config, the firewall rules,
# Tailscale, Funnel or ssh-watchdog.sh.
#
# Undo: sudo rm /etc/fail2ban/jail.d/opshub-sshd.local && sudo systemctl restart fail2ban
#       (or remove it completely with: sudo apt remove fail2ban)

set -e

JAIL_FILE="/etc/fail2ban/jail.d/opshub-sshd.local"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo:  sudo ./fail2ban-install.sh" >&2
  exit 1
fi

echo "Step 1 of 4 - fail2ban package"
if command -v fail2ban-client >/dev/null 2>&1; then
  echo "  already installed"
else
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban
  echo "  installed"
fi

echo "Step 2 of 4 - writing $JAIL_FILE"
cat > "$JAIL_FILE" <<'CONF'
# Written by pi-scripts/fail2ban-install.sh. Only the sshd jail is enabled.
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 100.64.0.0/10 192.168.0.0/16 10.0.0.0/8
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
backend = systemd
CONF
echo "  written (5 tries in 10 min -> 1 hour ban; localhost, Tailscale and local network never banned)"

echo "Step 3 of 4 - starting fail2ban"
systemctl enable fail2ban >/dev/null 2>&1 || true
systemctl restart fail2ban
sleep 2

echo "Step 4 of 4 - status"
fail2ban-client status sshd || echo "  (sshd jail not up yet - check: sudo journalctl -u fail2ban -n 30)"

echo
# Read-only look at the effective setting; nothing is changed.
PW=$(sshd -T 2>/dev/null | awk '$1=="passwordauthentication"{print $2}')
case "$PW" in
  yes) echo "SSH password login is currently ON (fail2ban helps protect it; turning it off is a separate decision)." ;;
  no)  echo "SSH password login is currently OFF (keys only)." ;;
  *)   echo "Couldn't tell whether SSH password login is on (sshd -T gave no answer)." ;;
esac
