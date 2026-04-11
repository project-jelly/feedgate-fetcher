#!/usr/bin/env bash
# Runs scripts/live_verify.py every N seconds until killed.
#
# Intended to be started via nohup so it survives the interactive
# shell that launched it:
#
#   nohup bash scripts/live_verify_loop.sh > var/verify-runs/loop.log 2>&1 &
#   disown
#
# Stop with:
#   pkill -f live_verify_loop.sh
#
# Configurable interval via LIVE_VERIFY_INTERVAL_SECONDS
# (default: 600 = 10 minutes).

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

INTERVAL_SECONDS="${LIVE_VERIFY_INTERVAL_SECONDS:-600}"
PY="$REPO_ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "FATAL: venv python not found at $PY" >&2
  echo "Run 'uv sync' first." >&2
  exit 1
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] live-verify loop starting (interval=${INTERVAL_SECONDS}s, pid=$$)"

while true; do
  "$PY" scripts/live_verify.py || echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] verifier exited with non-zero status"
  sleep "$INTERVAL_SECONDS"
done
