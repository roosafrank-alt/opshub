#!/bin/bash
# preview-install.sh - sets up a PREVIEW of OpsHub next to the live app, never instead of it.
#
# It makes two extra copies of OpsHub, each with its OWN copy of the live data:
#   after  = the new design, changes outlined in pink   -> port 5051
#   before = exactly what is live today                  -> port 5052
# The live app (port 5050, the opshub service, Tailscale Funnel) is only READ from
# (its code and a copy of its database). Nothing here changes it, restarts it or
# touches Tailscale. The previews cannot send emails, texts, phone alerts, Wave
# calls or restart anything, because they run on copies of real data.
#
# Run on the Pi as frank (it asks for your sudo password once, to add two services):
#   bash preview-install.sh --source-dir ~/opshub-branch     (code already on the Pi)
#   bash preview-install.sh --zip ~/branch.zip                (ZIP of the preview branch from GitHub)
#   GITHUB_TOKEN=... bash preview-install.sh                  (downloads the branch itself)
# Later:
#   bash preview-install.sh --refresh [same source option]    new code, KEEPS your preview data
#   bash preview-install.sh --reset-data                      fresh copy of today's live data
#   bash preview-install.sh --remove                          delete both previews and their services
#
# Then open (accept the browser certificate warning once per address and port):
#   https://<the Pi's Tailscale address>:5051      (works from anywhere, like ssh does)
#   https://opshub.local:5051                      (at home)
# and use "Side by side" in the pink bar at the top.
set -euo pipefail

LIVE="${LIVE_DIR:-$HOME/shopinv}"
AFTER="$HOME/shopinv-preview"
BEFORE="$HOME/shopinv-before"
BRANCH="preview/ui-review-2026-10"
REPO="roosafrank-alt/opshub"
PORT_AFTER=5051
PORT_BEFORE=5052
MODE="install"; SRC_DIR=""; SRC_ZIP=""; NO_SERVICE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --refresh) MODE="refresh" ;;
    --reset-data) MODE="reset-data" ;;
    --remove) MODE="remove" ;;
    --source-dir) SRC_DIR="$2"; shift ;;
    --zip) SRC_ZIP="$2"; shift ;;
    --no-service) NO_SERVICE=1 ;;   # testing only: do not install systemd units
    *) echo "Unknown option $1"; exit 2 ;;
  esac
  shift
done

say() { echo; echo "== $*"; }
die() { echo "STOP: $*" >&2; exit 1; }

# Never let this script point at the live folder or the live port.
[ "$AFTER" != "$LIVE" ] && [ "$BEFORE" != "$LIVE" ] || die "preview folders must differ from the live folder"
[ "$PORT_AFTER" != 5050 ] && [ "$PORT_BEFORE" != 5050 ] || die "5050 is the live port"

units() { echo opshub-preview opshub-before; }

if [ "$MODE" = "remove" ]; then
  say "Removing the previews (the live app is not touched)"
  for u in $(units); do
    sudo systemctl disable --now "$u" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$u.service"
  done
  sudo systemctl daemon-reload || true
  rm -rf "$AFTER" "$BEFORE"
  echo "Done. Live OpsHub was not changed."
  exit 0
fi

[ -f "$LIVE/instance/shopinv.db" ] || die "can't find the live database at $LIVE/instance/shopinv.db (set LIVE_DIR=... if the app lives elsewhere)"
command -v python3 >/dev/null || die "python3 not found"

copy_data() {  # copy_data <dest tree>: a consistent copy of the live data, never the live files themselves
  local dest="$1"
  mkdir -p "$dest/instance"
  python3 - "$LIVE/instance/shopinv.db" "$dest/instance/shopinv.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)   # safe while the live app is running
dst.close(); src.close()
PY
  for f in secret_key; do [ -f "$LIVE/instance/$f" ] && cp -p "$LIVE/instance/$f" "$dest/instance/$f"; done   # same key = one login works on both previews
  for f in cert.pem key.pem; do [ -f "$LIVE/$f" ] && cp -p "$LIVE/$f" "$dest/$f"; done
  if [ -d "$LIVE/static/uploads" ]; then mkdir -p "$dest/static"; rm -rf "$dest/static/uploads"; cp -a "$LIVE/static/uploads" "$dest/static/uploads"; fi
}

get_after_code() {
  local tmp; tmp="$(mktemp -d)"
  if [ -n "$SRC_DIR" ]; then
    [ -f "$SRC_DIR/run_preview.py" ] || die "$SRC_DIR doesn't look like the preview branch (no run_preview.py)"
    tar -C "$SRC_DIR" --exclude=./.git --exclude=./instance --exclude=./static/uploads -cf - . | tar -C "$tmp" -xf -
  elif [ -n "$SRC_ZIP" ]; then
    command -v unzip >/dev/null || die "unzip isn't installed (sudo apt install unzip)"
    unzip -q "$SRC_ZIP" -d "$tmp/z"; local top; top="$(ls -d "$tmp"/z/*/ | head -1)"
    tar -C "$top" -cf - . | tar -C "$tmp" -xf -; rm -rf "$tmp/z"
  else
    command -v git >/dev/null || die "git isn't installed"
    local url="https://github.com/$REPO"
    [ -n "${GITHUB_TOKEN:-}" ] && url="https://x-access-token:${GITHUB_TOKEN}@github.com/$REPO"
    GIT_TERMINAL_PROMPT=0 git clone -q --depth 1 --branch "$BRANCH" "$url" "$tmp/repo" || die "couldn't download branch $BRANCH. Use --zip or --source-dir instead (see the top of this script)."
    rm -rf "$tmp/repo/.git"; tar -C "$tmp/repo" -cf - . | tar -C "$tmp" -xf -; rm -rf "$tmp/repo"
  fi
  [ -f "$tmp/run_preview.py" ] || die "downloaded code has no run_preview.py"
  # keep the preview's own data and certs, replace only code
  mkdir -p "$AFTER"
  tar -C "$tmp" -cf - . | tar -C "$AFTER" -xf -
  rm -rf "$tmp"
  [ -d "$LIVE/vendor" ] && [ ! -e "$AFTER/vendor" ] && ln -s "$LIVE/vendor" "$AFTER/vendor"   # Flask lives in vendor on the Pi (read only)
  [ -d "$AFTER/vendor" ] || [ -d "$LIVE/vendor" ] || true
}

get_before_code() {  # exactly the code that is live right now
  mkdir -p "$BEFORE"
  tar -C "$LIVE" --exclude=./instance --exclude=./static/uploads --exclude=./__pycache__ -cf - . | tar -C "$BEFORE" -xf -
  [ -f "$AFTER/run_preview.py" ] || die "after-copy missing"
}

write_units() {
  local user; user="$(id -un)"
  for pair in "opshub-preview:$AFTER:$PORT_AFTER:preview" "opshub-before:$BEFORE:$PORT_BEFORE:before"; do
    IFS=: read -r name tree port label <<<"$pair"
    sudo tee "/etc/systemd/system/$name.service" >/dev/null <<UNIT
[Unit]
Description=OpsHub $label copy (not the live app)
After=network.target

[Service]
User=$user
WorkingDirectory=$tree
ExecStart=/usr/bin/env python3 $AFTER/run_preview.py --tree $tree --port $port --label $label --before-port $PORT_BEFORE --preview-port $PORT_AFTER
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  done
  sudo systemctl daemon-reload
}

ensure_key() {  # one session key for both previews so a single login works on both sides
  mkdir -p "$AFTER/instance" "$BEFORE/instance"
  if [ ! -s "$AFTER/instance/secret_key" ]; then
    python3 -c "import secrets;print(secrets.token_hex(32),end='')" > "$AFTER/instance/secret_key"; chmod 600 "$AFTER/instance/secret_key"
  fi
  cp -p "$AFTER/instance/secret_key" "$BEFORE/instance/secret_key"
}

start_services() {
  if [ "$NO_SERVICE" = 1 ]; then
    say "--no-service: starting both copies in the background for a test"
    (cd "$AFTER" && nohup python3 "$AFTER/run_preview.py" --tree "$AFTER" --port $PORT_AFTER --label preview >"$HOME/preview-after.log" 2>&1 &)
    (cd "$BEFORE" && nohup python3 "$AFTER/run_preview.py" --tree "$BEFORE" --port $PORT_BEFORE --label before >"$HOME/preview-before.log" 2>&1 &)
    return
  fi
  write_units
  for u in $(units); do sudo systemctl enable "$u" >/dev/null 2>&1; sudo systemctl restart "$u"; done
}

case "$MODE" in
  install)
    [ ! -e "$AFTER" ] || die "$AFTER already exists. Use --refresh (new code, keep data) or --reset-data."
    say "Getting the new design code"; get_after_code
    say "Copying today's live code for the 'before' side"; get_before_code
    say "Copying a snapshot of the live data into each preview (the live files are only read)"
    copy_data "$AFTER"; copy_data "$BEFORE"
    ;;
  refresh)
    [ -d "$AFTER" ] || die "run the install first"
    say "Updating the new design code (your preview data stays)"; get_after_code
    say "Updating the 'before' code to what is live right now"; get_before_code
    ;;
  reset-data)
    [ -d "$AFTER" ] || die "run the install first"
    say "Replacing both previews' data with a fresh snapshot of the live data"
    for u in $(units); do sudo systemctl stop "$u" 2>/dev/null || true; done
    copy_data "$AFTER"; copy_data "$BEFORE"
    ;;
esac

say "Starting"
ensure_key
start_services
sleep 3

TS=""; command -v tailscale >/dev/null && TS="$(tailscale ip -4 2>/dev/null | head -1 || true)"   # read only
LAN="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Preview is up. The live app was not changed."
echo "  New design : https://${TS:-$LAN}:$PORT_AFTER   (or https://opshub.local:$PORT_AFTER at home)"
echo "  Before     : https://${TS:-$LAN}:$PORT_BEFORE"
echo "Log in with your normal username and password. Open each address once and accept the certificate warning."
echo "Side by side: tap 'Side by side' in the pink bar at the top of any preview page."
