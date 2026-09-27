# Notes for Claude sessions working on OpsHub

This applies to every session: the Idea Queue runner, the nightly reviewers,
repair and fix sessions, and ad-hoc chats.

## Keep Tailscale Funnel on

Phones reach OpsHub at https://opshub.taila1bcc5.ts.net through **Tailscale
Funnel**, with no Wi-Fi or Tailscale app needed. If Funnel goes off, every phone
gets "cannot establish a secure connection", even though the app on the Pi is
running fine. That happened on Sep 27, 2026.

- Never run `tailscale serve ...`, `tailscale funnel ... off`, `tailscale serve reset`
  or `tailscale down` on the Pi, and never put them in scripts, deploy steps or
  instructions for Frank. A plain `tailscale serve` command **replaces** the Funnel
  setting with "tailnet only".
- If Funnel has to be set up again, the only correct command is
  `sudo tailscale funnel --bg https+insecure://127.0.0.1:5050`.
  `tailscale funnel status` must then show `(Funnel on)`.
- `pi-scripts/funnel-watchdog.sh` (root cron, every 5 minutes) turns Funnel back
  on if anything switches it off, and `pi-scripts/opshub-uptime.sh` alerts if it's
  off. Don't remove or weaken either one.
- The app must keep serving HTTPS on port 5050 (`cert.pem` and `key.pem` in the
  app folder, see the bottom of `app.py`), because Funnel proxies to
  `https+insecure://127.0.0.1:5050`. Don't change the port or switch the app to
  plain HTTP.
