#!/bin/bash
# opshub-security-check.sh - daily "are the security checks still happening?"
# check for the OpsHub Pi. Installed to /usr/local/sbin/opshub-security-check by
# security-automation-install.sh and run by opshub-security-check.timer
# (06:40 AM, right after the 6:30 backup watchdog).
#
# It looks at four things:
#   1. the nightly virus scan finished within the last 36 hours
#   2. the virus signatures (ClamAV database) were updated within 4 days
#   3. the weekly rootkit check finished within the last 9 days
#   4. automatic security updates ran within 3 days, and no security update has
#      been waiting more than 3 days
#   (plus: neither scan service is sitting in a "failed" state)
#
# How Frank gets told:
#   - Any problem  -> phone push (ntfy, same topic as the other OpsHub alerts) AND
#                     a /fail ping to Healthchecks.io
#   - All fine     -> success ping to Healthchecks.io
#   - This check never runs (Pi off, timer broken) -> Healthchecks.io gets no ping
#                     and alerts on its own after the grace time.
#
# Healthchecks.io settings for this check ("OpsHub security"): Period 1 day,
# Grace Time 3 hours. The ping address is kept in /etc/opshub-security/hc-url
# (root only, never in this repo).
#
# Read-only: it changes nothing except its own log and one marker file.

STATE=/var/lib/opshub-security
HC_FILE=/etc/opshub-security/hc-url
LOG=/var/log/opshub-security.log
NTFY_URL_FILE=/home/frank/.opshub-ntfy-url

mkdir -p "$STATE"
HC_URL=""
[ -r "$HC_FILE" ] && HC_URL=$(tr -d '[:space:]' < "$HC_FILE")

log() { echo "$(date -Iseconds) $1" >> "$LOG"; }

# Whole hours since a file changed; 99999 if it doesn't exist.
age_hours() {
  if [ -e "$1" ]; then
    echo $(( ( $(date +%s) - $(stat -c %Y "$1") ) / 3600 ))
  else
    echo 99999
  fi
}

# A brand-new install gets a grace period: use whichever is more recent, the
# marker file or the install time.
eff_age() {
  a=$(age_hours "$1"); i=$(age_hours "$STATE/installed")
  if [ "$a" -lt "$i" ]; then echo "$a"; else echo "$i"; fi
}

problems=()

clam=$(eff_age "$STATE/clamscan.ok")
[ "$clam" -le 36 ] || problems+=("virus scan has not finished in $clam hours")

sigfile=$(ls -t /var/lib/clamav/daily.cld /var/lib/clamav/daily.cvd 2>/dev/null | head -n 1)
sig=$(age_hours "${sigfile:-/nonexistent}")
i=$(age_hours "$STATE/installed")
[ "$sig" -lt "$i" ] && siga=$sig || siga=$i
[ "$siga" -le 96 ] || problems+=("virus signatures not updated in $siga hours")

rk=$(eff_age "$STATE/rkhunter.ok")
[ "$rk" -le 216 ] || problems+=("rootkit check has not finished in $rk hours")

ua=$(eff_age /var/lib/apt/periodic/unattended-upgrades-stamp)
[ "$ua" -le 72 ] || problems+=("automatic security updates have not run in $ua hours")

pending=$(apt-get -s dist-upgrade 2>/dev/null | grep -c '^Inst .*-security')
if [ "${pending:-0}" -gt 0 ]; then
  [ -e "$STATE/security-pending-since" ] || touch "$STATE/security-pending-since"
  pa=$(age_hours "$STATE/security-pending-since")
  [ "$pa" -le 72 ] || problems+=("$pending security update(s) waiting for $pa hours")
else
  rm -f "$STATE/security-pending-since"
fi

for u in opshub-clamscan opshub-rkhunter; do
  systemctl is-failed --quiet "$u.service" && problems+=("$u failed on its last run")
done

hc_ping() {  # "" for success, "/fail" for failure, then optional message
  [ -n "$HC_URL" ] || return 0
  curl -fsS --max-time 15 --retry 3 --data-raw "${2:-ok}" "$HC_URL$1" -o /dev/null 2>>"$LOG" || log "healthchecks ping failed"
}

notify() {  # title message priority tags
  [ -r "$NTFY_URL_FILE" ] || return 0
  url=$(tr -d '[:space:]' < "$NTFY_URL_FILE")
  [ -n "$url" ] || return 0
  curl -fsS -m 15 -H "Title: $1" -H "Priority: ${3:-default}" -H "Tags: ${4:-}" -d "$2" "$url" >/dev/null 2>&1 || true
}

if [ "${#problems[@]}" -gt 0 ]; then
  msg=$(printf '%s; ' "${problems[@]}")
  log "PROBLEM: $msg"
  notify "OpsHub: security checks need attention" "$msg" high warning
  hc_ping /fail "$msg"
  exit 1
fi

log "ok: scan ${clam}h, signatures ${siga}h, rootkit ${rk}h, updates ${ua}h, pending security ${pending:-0}"
hc_ping "" "ok"

tail -n 400 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
exit 0
