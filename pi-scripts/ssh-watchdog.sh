#!/bin/bash
# ssh-watchdog.sh - checks that sshd is actually accepting connections and
# restarts it if not. Runs as root via cron every 15 minutes.
#
# Install:
#   sudo cp ssh-watchdog.sh /usr/local/sbin/ssh-watchdog.sh
#   sudo chmod +x /usr/local/sbin/ssh-watchdog.sh
#   sudo crontab -e
#     */15 * * * * /usr/local/sbin/ssh-watchdog.sh >> /var/log/ssh-watchdog.log 2>&1
#
# Logic: systemctl is-active is fast but can lie if the service is "active"
# yet wedged/not actually listening, so this also does a real TCP check
# against port 22 with a short timeout before deciding sshd is healthy.

LOG_TAG="ssh-watchdog"

is_port_open() {
  timeout 3 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/22" 2>/dev/null
}

SERVICE_OK="no"
if systemctl is-active --quiet ssh; then
  SERVICE_OK="yes"
fi

if [ "$SERVICE_OK" = "yes" ] && is_port_open; then
  # Healthy - nothing to do (log line kept minimal to avoid log spam)
  exit 0
fi

logger -t "$LOG_TAG" "sshd unhealthy (service_active=$SERVICE_OK, port22_open=$(is_port_open && echo yes || echo no)) - restarting"
systemctl restart ssh
sleep 3

if systemctl is-active --quiet ssh && is_port_open; then
  logger -t "$LOG_TAG" "restart succeeded, sshd healthy again"
else
  logger -t "$LOG_TAG" "restart did not bring sshd back - manual/physical intervention likely needed"
fi
