#!/usr/bin/env bash
# Stops the overnight verification stack cleanly.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
VAR_DIR="$REPO_ROOT/var/verify-runs"

for name in verify_loop uvicorn; do
  pidfile="$VAR_DIR/$name.pid"
  if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      echo "stopping $name (pid $pid)"
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  fi
done

# Mop up any stragglers
pkill -f live_verify_loop.sh 2>/dev/null || true
pkill -f "uvicorn feedgate.main:create_app" 2>/dev/null || true

docker compose down 2>/dev/null || true

echo "done."
