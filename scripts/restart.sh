#!/usr/bin/env bash
# Restart the local dev server on port 8000.
set -u
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"

# Windows has no pkill; find the PID listening on the port and kill it.
if command -v netstat >/dev/null 2>&1; then
  PIDS=$(netstat -ano 2>/dev/null | grep -E "LISTENING" | grep ":$PORT " | awk '{print $NF}' | sort -u)
  for pid in $PIDS; do
    echo "stopping pid $pid on port $PORT"
    taskkill //F //PID "$pid" >/dev/null 2>&1 || true
  done
fi
sleep 1

: > server.log
python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >> server.log 2>&1 &
echo "started; waiting for it to come up..."

for _ in $(seq 1 30); do
  if curl -sS --max-time 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "ready on http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 1
done

echo "did not come up; last log lines:"
tail -20 server.log
exit 1
