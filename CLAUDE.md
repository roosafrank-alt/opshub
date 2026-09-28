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

## Frank's Pi cheat sheet

Frank asked for these to be kept here because he forgets them. When he asks how to
get onto the Pi or run the tests, give him these exact commands.

- **Log in to the Pi at home** (Mac on the same network as the Pi; finds the Pi by
  name, so it keeps working even if its local `192.168.9.x` address changes):
  `ssh frank@opshub.local`
- **Log in to the Pi from anywhere** (via Tailscale):
  `ssh frank@100.101.116.22`
- **Run the test suite on the Pi** (the app keeps Flask in its own `vendor`
  folder, so plain `python3` fails with "No module named 'flask'" without the
  `PYTHONPATH` part):
  `cd ~/shopinv && PYTHONPATH=/home/frank/shopinv/vendor python3 -m unittest discover -s tests`
  To keep a copy of the output, add `2>&1 | tee ~/test-run.log | tail -40`.
  It takes about 3 minutes and should end with `OK`.
- The app lives in `~/shopinv` on the Pi (not `~/opshub`) and runs as the
  `opshub` systemd service.

## Keep the Idea Queue runner copies in step

The **Idea Queue Runner** routine holds the real runner instructions in its own
prompt. **Idea Queue Runner 17** and **Idea Queue Runner 47** (and any copy added
later) don't: their prompt just says to read `idea-queue/runner-instructions.md`
from main and follow it.

- When you change the Idea Queue Runner's instructions, make the same change in
  `idea-queue/runner-instructions.md`, so the copies stay in step. The file must
  always match the original routine's prompt, plus the THIS FILE rule at the end.
- If Frank says "sync the runner file", compare the original routine's prompt
  with the file and bring the file back in line with the prompt.
- The **Idea Queue Runner** routine was created through the API, not by an agent, so
  `update_trigger` refuses to change its prompt ("Agents can only update routines they
  created"). Only Frank can edit it, at
  https://claude.ai/code/routines/trig_01FMiUsrsJWLuJxRr9saRxMS. So when a change to the
  runner instructions is made from a session: push the file to main (the copies read it
  from there) and ask Frank to paste the same text into that routine's prompt. Don't
  delete and recreate the routine to get around it - that loses its history, and the page's
  Wake / Deploy / Roll back buttons fire it by that exact id.
- Known-good backups from before this was set up (Sep 27, 2026) are the branches
  `backup/main-2026-09-27` and `backup/idea-queue-2026-09-27`.
