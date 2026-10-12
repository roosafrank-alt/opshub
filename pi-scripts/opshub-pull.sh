#!/bin/bash
# opshub-pull.sh - installs OpsHub on the Pi from GitHub. Every 2 minutes it
# looks at the main branch of https://github.com/roosafrank-alt/opshub and,
# when main has moved, puts that exact commit into ~/shopinv and restarts the
# opshub service. This is the "The Pi installs main within about 2 minutes"
# that the Idea Queue's Deploy to Pi, Roll back and Undo buttons rely on:
# they only ever push to main, and this script is what makes the push land.
#
# Install: see "How code reaches the Pi" in CLAUDE.md. In short: ~/shopinv
# must be a git checkout of main (instance/, vendor/ and the cert files are
# gitignored, so turning the folder into a checkout leaves them alone), frank
# must be allowed to run exactly "systemctl restart opshub" without a
# password, and this file runs from frank's crontab every 2 minutes:
#   mkdir -p ~/pi-scripts && cp opshub-pull.sh ~/pi-scripts/
#   chmod +x ~/pi-scripts/opshub-pull.sh
#   crontab -e
#     */2 * * * * $HOME/pi-scripts/opshub-pull.sh
#
# Undo (the Pi then stays on whatever commit it has):
#   crontab -e   # delete the line above
#   rm -rf ~/pi-scripts/opshub-pull.sh ~/.opshub-pull ~/.opshub-pull.log
#
# What one run does, in order:
#   1. git fetch origin main. Nothing changed -> exit quietly (the usual case).
#   2. Hand edits in ~/shopinv (tracked files that differ from the commit the
#      Pi is on) are saved as a .diff in ~/shopinv-backups and then replaced:
#      main is the only source of truth, edits belong in a branch on GitHub.
#   3. Check out main. If requirements.txt changed, pip install into vendor/.
#   4. tools/predeploy_check.py must print OK (syntax, routes, templates,
#      db.py rebuild guards). If not, the previous commit goes back and Frank
#      gets one phone alert. Nothing is restarted.
#   5. Restart the opshub service and wait for the login page to answer on
#      port 5050. If it doesn't within a minute, the previous commit goes
#      back, the service is restarted again, and Frank gets one phone alert.
#   A commit that failed is remembered in ~/.opshub-pull/bad-sha, so it is
#   not retried (and not re-alerted) every 2 minutes. The next push to main,
#   including the Idea Queue's Roll back, is tried again normally.
#
# Alerts go to the same ntfy topic as pi_health.py (~/.opshub-ntfy-url, kept
# only on the Pi). Successful installs are just logged in ~/.opshub-pull.log;
# the Idea Queue page already tells Frank what was sent.
#
# Runs from frank's own crontab. The one thing that needs root is restarting
# the service, so frank needs a sudoers rule for exactly
# "systemctl restart opshub" and nothing else (see CLAUDE.md).

APP_DIR="$HOME/shopinv"
REMOTE="origin"
BRANCH="main"
SERVICE="opshub"
PORT=5050
STATE_DIR="$HOME/.opshub-pull"
LOG_FILE="$HOME/.opshub-pull.log"
LOG_MAX_LINES=500
NTFY_URL_FILE="$HOME/.opshub-ntfy-url"
BACKUP_DIR="$HOME/shopinv-backups"
START_WAIT_SECONDS=60

mkdir -p "$STATE_DIR"

trim_log() {
  [ -f "$LOG_FILE" ] || return 0
  tail -n "$LOG_MAX_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
}

log() {
  echo "$(date -Iseconds) $1" >> "$LOG_FILE"
  trim_log
}

# Best-effort phone push, same topic as pi_health.py. The log is the record
# of truth if this fails.
alert() {
  local url=""
  [ -r "$NTFY_URL_FILE" ] && url=$(tr -d '[:space:]' < "$NTFY_URL_FILE")
  if [ -z "$url" ]; then
    log "no ntfy topic in $NTFY_URL_FILE - alert kept in this log only"
    return 0
  fi
  curl -fsS --max-time 10 -H "Title: OpsHub deploy" --data-raw "$1" "$url" -o /dev/null 2>/dev/null
}

# One run at a time: a slow pip install or restart must not overlap the next
# cron tick.
exec 9>"$STATE_DIR/lock"
if ! flock -n 9; then
  exit 0
fi

git_app() { git -C "$APP_DIR" "$@"; }

if [ ! -d "$APP_DIR/.git" ]; then
  if [ ! -f "$STATE_DIR/no-git-warned" ]; then
    log "$APP_DIR is not a git checkout - see How code reaches the Pi in CLAUDE.md. Nothing installed."
    alert "OpsHub Pi: ~/shopinv is not a git checkout yet, so deploys from the Idea Queue are NOT reaching the Pi. See How code reaches the Pi in CLAUDE.md."
    touch "$STATE_DIR/no-git-warned"
  fi
  exit 1
fi
rm -f "$STATE_DIR/no-git-warned"

if ! timeout 90 git -C "$APP_DIR" fetch --quiet "$REMOTE" "$BRANCH" 2>>"$LOG_FILE"; then
  log "git fetch failed (no network?) - will try again in 2 minutes"
  trim_log
  exit 1
fi

NEW=$(git_app rev-parse "$REMOTE/$BRANCH" 2>/dev/null)
CUR=$(git_app rev-parse HEAD 2>/dev/null)
if [ -z "$NEW" ] || [ -z "$CUR" ]; then
  log "could not read commits in $APP_DIR (NEW='$NEW', CUR='$CUR')"
  exit 1
fi

# Already on main: the normal, quiet case.
if [ "$NEW" = "$CUR" ]; then
  exit 0
fi

# A commit that already failed here is not retried; wait for the next push.
if [ -f "$STATE_DIR/bad-sha" ] && [ "$(cat "$STATE_DIR/bad-sha")" = "$NEW" ]; then
  exit 0
fi

short_new=$(git_app rev-parse --short "$NEW")
short_cur=$(git_app rev-parse --short "$CUR")
subject=$(git_app log -1 --format=%s "$NEW")

# Hand edits on the Pi: keep a copy, then let main win.
if [ -n "$(git_app status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  mkdir -p "$BACKUP_DIR"
  diff_file="$BACKUP_DIR/pi-edits-$(date +%Y%m%d-%H%M%S).diff"
  git_app diff > "$diff_file" 2>/dev/null
  log "files in $APP_DIR had been edited on the Pi; saved them to $diff_file and replaced them with main"
  alert "OpsHub Pi: files in ~/shopinv were edited on the Pi and have been replaced by main. The edits are saved in $diff_file. Edits need to go through a branch on GitHub."
fi

fail_install() {
  # $1 = short reason for the log and the alert. Puts the previous commit
  # back and remembers NEW so this isn't repeated every 2 minutes.
  local reason="$1"
  git_app reset --hard --quiet "$CUR" 2>>"$LOG_FILE"
  echo "$NEW" > "$STATE_DIR/bad-sha"
  log "FAILED to install $short_new ($subject): $reason. Back on $short_cur."
  alert "OpsHub deploy $short_new NOT installed: $reason. The Pi is back on $short_cur. Press Roll back or Have Claude fix it in the Idea Queue."
  exit 1
}

log "installing $short_new (was $short_cur): $subject"
if ! git_app reset --hard --quiet "$NEW" 2>>"$LOG_FILE"; then
  fail_install "git could not check out the new commit"
fi

# New or changed dependencies go into the app's own vendor/ folder (the app
# has no system-wide Flask; see the test command in CLAUDE.md).
if ! git_app diff --quiet "$CUR" "$NEW" -- requirements.txt 2>/dev/null; then
  log "requirements.txt changed - installing into vendor/"
  if ! timeout 900 python3 -m pip install --quiet --upgrade --target "$APP_DIR/vendor" -r "$APP_DIR/requirements.txt" >>"$LOG_FILE" 2>&1; then
    fail_install "pip install of the changed requirements.txt into vendor/ failed (see ~/.opshub-pull.log)"
  fi
fi

check_output=$(cd "$APP_DIR" && PYTHONPATH="$APP_DIR/vendor" timeout 120 python3 tools/predeploy_check.py 2>&1)
if [ "$?" != "0" ]; then
  first_line=$(echo "$check_output" | grep -v '^OK' | head -n 2 | tr '\n' ' ')
  echo "$check_output" >> "$LOG_FILE"
  fail_install "safety check failed: ${first_line:-see ~/.opshub-pull.log}"
fi

restart_service() {
  sudo -n systemctl restart "$SERVICE" 2>>"$LOG_FILE"
}

# The Pi serves HTTPS with its own cert when cert.pem/key.pem exist, plain
# HTTP otherwise; either counts as up. The login page answers 200.
page_up() {
  local code
  code=$(curl -k -s -o /dev/null -w "%{http_code}" --max-time 5 "https://127.0.0.1:$PORT/" 2>/dev/null)
  [ "$code" = "200" ] && return 0
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:$PORT/" 2>/dev/null)
  [ "$code" = "200" ]
}

wait_for_app() {
  local waited=0
  while [ "$waited" -lt "$START_WAIT_SECONDS" ]; do
    sleep 3
    waited=$((waited + 3))
    page_up && return 0
  done
  return 1
}

if ! restart_service; then
  fail_install "could not restart the $SERVICE service (sudo rule for systemctl restart opshub missing? see CLAUDE.md)"
fi

if wait_for_app; then
  log "live on $short_new: $subject"
  exit 0
fi

# The new code did not come up. Put the old one back and start it again.
journal=$(journalctl -u "$SERVICE" -n 15 --no-pager 2>/dev/null | tail -n 15)
[ -n "$journal" ] && printf '%s\n' "--- $SERVICE log after failed start of $short_new ---" "$journal" "---" >> "$LOG_FILE"
git_app reset --hard --quiet "$CUR" 2>>"$LOG_FILE"
echo "$NEW" > "$STATE_DIR/bad-sha"
restart_service
if wait_for_app; then
  log "FAILED to start $short_new ($subject); $short_cur is back up. Service log is above in this file."
  alert "OpsHub deploy $short_new did not start (service: $(systemctl is-active "$SERVICE" 2>/dev/null)). The Pi is back on $short_cur and running. Press Roll back or Have Claude fix it in the Idea Queue; ~/.opshub-pull.log has the error."
else
  log "FAILED to start $short_new AND $short_cur did not come back up - OpsHub is DOWN"
  alert "OpsHub is DOWN: deploy $short_new did not start and the previous version $short_cur did not come back either. ssh frank@opshub.local, then: sudo systemctl status opshub; tail -40 ~/.opshub-pull.log"
fi
exit 1
