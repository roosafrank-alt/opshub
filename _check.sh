#!/bin/bash
# Idea Queue pre-deploy check.
# Usage (from ~/Desktop/shopinv_queue):  bash _check.sh file1 file2 ...
# Nothing is copied anywhere by this script. It exits non-zero on any problem,
# so a command chained after it with && never runs.
set -u
Q="$(cd "$(dirname "$0")" && pwd)"
D="$(dirname "$Q")"
N="$(ls -d "$D"/shopinv_v* 2>/dev/null | sed 's/.*shopinv_v//' | grep -E '^[0-9]+$' | sort -n | tail -1)"
MAIN="$D/shopinv_v$N"
[ -n "$N" ] && [ -d "$MAIN" ] || { echo "✗ Can't find your main copy (shopinv_vN) on the Desktop."; exit 2; }
[ $# -gt 0 ] || { echo "✗ No files given."; exit 2; }
fail=0
echo "Checking the queue's changes against $(basename "$MAIN")..."

# 1) Was the main copy edited (e.g. by the other chat) after the queue built this change?
for f in "$@"; do
  if [ ! -f "$Q/$f" ]; then echo "✗ Missing in queue: $f"; fail=1; continue; fi
  if [ -f "$MAIN/$f" ] && [ -f "$Q/_synced/$f" ]; then
    if ! cmp -s "$MAIN/$f" "$Q/_synced/$f" && ! cmp -s "$MAIN/$f" "$Q/$f"; then
      echo "✗ $f was changed in $(basename "$MAIN") after the queue made its change. Copying would wipe that edit."
      fail=1
    fi
  fi
done
if [ $fail -ne 0 ]; then
  echo
  echo "STOPPED - nothing was copied. Requeue this idea so Claude merges the newer edits first."
  exit 1
fi

# 2) Build a throwaway test copy = main copy + these changes, then run the full app check on it.
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
rsync -a --exclude instance --exclude 'static/uploads' --exclude __pycache__ --exclude '*.pem' --exclude .DS_Store "$MAIN/" "$T/"
( cd "$Q" && rsync -aR "$@" "$T/" )
if [ -f "$T/tools/predeploy_check.py" ]; then
  if ! python3 "$T/tools/predeploy_check.py"; then
    echo
    echo "STOPPED - nothing was copied. The combined code has the problem above."
    exit 1
  fi
else
  for f in "$@"; do case "$f" in *.py) python3 -m py_compile "$T/$f" || fail=1;; esac; done
  [ $fail -eq 0 ] || { echo "STOPPED - nothing was copied. Syntax error above."; exit 1; }
  echo "OK (syntax check only - tools/predeploy_check.py not found)"
fi
echo "✓ Checks passed - safe to copy."
