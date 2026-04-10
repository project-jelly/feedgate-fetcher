#!/usr/bin/env bash
# Brings up the full overnight verification stack:
#   1. Postgres via docker compose
#   2. Alembic migrations
#   3. uvicorn (detached via nohup, logs -> var/verify-runs/uvicorn.log)
#   4. Seed feed registration
#   5. Live verify loop (detached via nohup, logs -> var/verify-runs/loop.log)
#
# Safe to re-run — every step is idempotent. PIDs land in
# var/verify-runs/*.pid so the teardown script can find them.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VAR_DIR="$REPO_ROOT/var/verify-runs"
mkdir -p "$VAR_DIR"

export FEEDGATE_DATABASE_URL="${FEEDGATE_DATABASE_URL:-postgresql+asyncpg://postgres:postgres@localhost:55432/feedgate}"
export FEEDGATE_FETCH_INTERVAL_SECONDS="${FEEDGATE_FETCH_INTERVAL_SECONDS:-60}"
export FEEDGATE_VERIFY_DSN="${FEEDGATE_VERIFY_DSN:-postgresql://postgres:postgres@localhost:55432/feedgate}"
export FEEDGATE_API_BASE="${FEEDGATE_API_BASE:-http://127.0.0.1:8765}"

PY="$REPO_ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "FATAL: venv python not found at $PY — run 'uv sync' first." >&2
  exit 1
fi

echo "== 1. docker compose up -d postgres =="
docker compose up -d postgres

echo "== 2. wait for postgres healthy =="
for i in $(seq 1 30); do
  s=$(docker inspect -f '{{.State.Health.Status}}' feedgate-pg 2>/dev/null || echo "unknown")
  echo "  try $i: $s"
  if [ "$s" = "healthy" ]; then break; fi
  sleep 1
done
if [ "$s" != "healthy" ]; then
  echo "FATAL: postgres did not become healthy" >&2
  exit 1
fi

echo "== 3. alembic upgrade head =="
"$PY" -m alembic upgrade head

echo "== 4. start uvicorn (detached) =="
if [ -f "$VAR_DIR/uvicorn.pid" ] && kill -0 "$(cat "$VAR_DIR/uvicorn.pid")" 2>/dev/null; then
  echo "  uvicorn already running (pid $(cat "$VAR_DIR/uvicorn.pid"))"
else
  nohup "$PY" -m uvicorn feedgate.main:create_app --factory \
    --host 127.0.0.1 --port 8765 --log-level info \
    > "$VAR_DIR/uvicorn.log" 2>&1 &
  echo $! > "$VAR_DIR/uvicorn.pid"
  disown || true
  echo "  uvicorn pid=$(cat "$VAR_DIR/uvicorn.pid")"
fi

echo "== 5. wait for uvicorn ready =="
READY=0
for i in $(seq 1 30); do
  if curl -sS -o /dev/null "$FEEDGATE_API_BASE/healthz"; then
    echo "  ready (try $i)"
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  echo "FATAL: uvicorn did not become ready" >&2
  tail -30 "$VAR_DIR/uvicorn.log" >&2 || true
  exit 1
fi

echo "== 6. register seed feeds =="
"$PY" scripts/seed_feeds.py

echo "== 7. start live verify loop (detached) =="
if [ -f "$VAR_DIR/verify_loop.pid" ] && kill -0 "$(cat "$VAR_DIR/verify_loop.pid")" 2>/dev/null; then
  echo "  verify loop already running (pid $(cat "$VAR_DIR/verify_loop.pid"))"
else
  nohup bash scripts/live_verify_loop.sh \
    > "$VAR_DIR/loop.log" 2>&1 &
  echo $! > "$VAR_DIR/verify_loop.pid"
  disown || true
  echo "  verify loop pid=$(cat "$VAR_DIR/verify_loop.pid")"
fi

echo ""
echo "== DONE =="
echo "Logs:"
echo "  uvicorn:     $VAR_DIR/uvicorn.log"
echo "  verify loop: $VAR_DIR/loop.log"
echo "  JSON runs:   $VAR_DIR/*.json"
echo ""
echo "Tail the loop log with:  tail -f $VAR_DIR/loop.log"
echo "Stop everything with:    bash scripts/overnight_teardown.sh"
