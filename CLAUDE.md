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

## How code reaches the Pi

Since Oct 3, 2026 the Pi installs **main** from GitHub by itself. Nothing is rsynced
from the Mac any more, and `~/Desktop/shopinv_v10` is no longer the source of anything.

- `pi-scripts/opshub-pull.sh` runs from frank's crontab every 2 minutes. When main has
  moved it checks out that commit in `~/shopinv`, pip-installs into `vendor/` if
  `requirements.txt` changed, runs `tools/predeploy_check.py`, restarts the `opshub`
  service and waits for the login page on port 5050. If the check fails or the app
  doesn't come up within a minute, it puts the previous commit back, restarts again and
  sends Frank one ntfy alert (same topic as `pi_health.py`, from `~/.opshub-ntfy-url`).
  A commit that failed is remembered in `~/.opshub-pull/bad-sha` and not retried until
  main moves again.
- So **main is production**: a push or a merged PR to main is live within 2 minutes.
  The Idea Queue's Deploy to Pi, Roll back and Undo buttons work by pushing to main and
  rely on this. Reviewers push only to `qa-tests`; the runner's work waits on
  `idea-queue` until Frank presses Deploy.
- **Never edit files in `~/shopinv` on the Pi** and never rsync into it. The puller
  replaces tracked files with main within 2 minutes (it saves a `.diff` of the edits in
  `~/shopinv-backups` first). `instance/`, `vendor/`, `cert.pem` and `key.pem` are
  gitignored, so pulls never touch the database, the secret key, the error logs or the
  certificate.
- The script needs three things on the Pi, all set up by Frank by hand: `~/shopinv` is a
  git checkout of main with `origin` = https://github.com/roosafrank-alt/opshub; frank
  may run exactly `systemctl restart opshub` with sudo and no password (a one-line file
  in `/etc/sudoers.d/`, nothing broader); and the cron line from the top of
  `opshub-pull.sh`. The repo is public, so no token is needed to fetch.
- Log: `~/.opshub-pull.log` on the Pi. What is live: `cd ~/shopinv && git log -1 --oneline`.
- Before Oct 3, 2026 the same job was done by `~/opshub-deploy.sh` on the Pi (its own
  checkout in `~/opshub-git`, rsynced into `~/shopinv`, log in `~/opshub-deploy.log`, ten
  code snapshots in `~/opshub-deploy-backups`). Its cron line is commented out, not
  deleted, so it can be turned back on if the new script ever misbehaves. Don't run both:
  they would restart the app twice for every deploy.

## Where backups are (nothing depends on the Mac)

Since Oct 3, 2026 the Mac holds no copy that matters. `~/Desktop/shopinv_v10` is an old
snapshot nobody updates; it can be deleted once Frank has checked it holds nothing
uncommitted.

- **Code:** GitHub (https://github.com/roosafrank-alt/opshub) holds the full history, and
  the Pi's `~/shopinv` is a complete checkout. To restore, clone the repo.
- **Data** (`instance/`: database, secret key, error logs) is not in git. It is backed up
  every night in three places, and `pi-scripts/backup-watchdog.sh` checks all three each
  morning: the Pi (`~/shopinv-backups`, 3:00 AM), the USB drive (`/mnt/backupdrive`,
  3:00 and 3:15 AM) and Backblaze B2 (offsite, 3:00 AM).
- **Not in git or in those nightly backups:** `cert.pem` and `key.pem` (a lost Pi just
  needs new ones), `~/.opshub-ntfy-url`, `~/.opshub-uptime-url` and
  `~/.backup-watchdog.conf`. Their values belong in the Access codes doc.

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
- **See what is live on the Pi** (the commit the Pi is running, which should match
  main on GitHub within 2 minutes of a deploy):
  `cd ~/shopinv && git log -1 --oneline`
- **See what the puller did or why a deploy didn't land**:
  `tail -20 ~/.opshub-pull.log`
  A line starting `FAILED` means the Pi went back to the previous commit; press Roll
  back or Have Claude fix it in the Idea Queue.

## Keep the Idea Queue runner copies in step

Since Sep 29, 2026 the runner works in two steps, to save usage on the many runs
that find nothing to do:

- **Idea Queue Runner** (:02) and **Idea Queue Runner 17**, **32** and **47** run on a
  cheap model. Their prompt just says to read `idea-queue/runner-quick-check.md` from
  main and follow it. That small file checks the queue and, only when there is work,
  fires the **Idea Queue Runner (page button)** routine (trig_01Kp15uGZfSdqeWMGiTUXo5n).
- **Idea Queue Runner (page button)** runs on the full model and follows the full rule
  book, `idea-queue/runner-instructions.md`, from main.
- So `idea-queue/runner-instructions.md` on main is the one real copy of the runner
  instructions. Change it (and `runner-quick-check.md`) only when Frank asks, and push
  to main so every routine picks it up. No routine prompt holds a pasted copy any more.
- The four scheduled runner routines were created through the API, not by an agent, so
  `update_trigger` refuses to change their prompts ("Agents can only update
  routines they created"). Only Frank can edit them (prompt and model), e.g. the :02 one at
  https://claude.ai/code/routines/trig_01FMiUsrsJWLuJxRr9saRxMS. Don't delete and
  recreate them to get around it - that loses their history, and the page's
  Wake / Deploy / Roll back buttons fire the :02 one by that exact id.
- The **Stuck Job Watchdog** routine (hourly) follows `idea-queue/stuck-job-watchdog.md`
  from main. It works only from the Idea Queue's meta/runner lock (routines can't list
  routines or stop other sessions): a heartbeat stale 30+ minutes gets one phone alert to
  Frank, and one stale 90+ minutes gets the lock cleared. Don't remove it or loosen its
  limits unless Frank asks.
- Known-good backups from before this was set up (Sep 27, 2026) are the branches
  `backup/main-2026-09-27` and `backup/idea-queue-2026-09-27`.

## Access codes sheet

Frank keeps every access code (tokens, passwords, keys, write keys) in one private
Claude Doc, **Access codes**: https://claude.ai/code/artifact/2aafcc06-f186-4745-b96a-ba7ce557ac5e

- When Frank asks for a code, read it from that doc with the Claude Docs tools.
- When an access code comes up in a chat, add it to that doc (what it is, the code, the
  date, how to get it again). If it changed, replace the old row.
- Codes go only in that doc, never in this repo, a commit, a routine prompt or a page.

## Keep app names the same on the Forge dashboard and the Idea Queue

The Forge start page tiles (`System/start-page/site/index.html`) and the Idea Queue's
project list (the `projects` collection in the Idea Queue artifact,
https://claude.ai/artifact/2oet1983QipXASSdbkEBfS) are two separate lists with nothing
linking them, so a rename in one never reaches the other. Whenever you rename an app in
either place, rename it in the other in the same session (update the project's `name`
with ArtifactData, pinned to its version). Tile id -> Idea Queue project id:
`trader` -> paper-trader, `realestate` -> realestate, `maintenance` -> equipment-maintenance,
`sellfinder` -> sell-finder, `marketface` -> marketface, `dinner` -> whats-for-dinner,
`jobsheet` -> up-to-you (shown as "Jobsheet" since Oct 2, 2026), `cleareddirect` -> none yet.
Add a row here when a new app gets a tile. Frank asked for this on Oct 2, 2026.
