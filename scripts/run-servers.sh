#!/usr/bin/env bash
# Start the four StayWise MCP servers (Ctrl-C stops them all). Logs go to data/logs/.
#
#   scripts/run-servers.sh            # reachable from this Mac and from the Agent Manager gateway (192.168.5.2)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
GATEWAY_HOST="${GATEWAY_HOST:-192.168.5.2}"     # how the k3d gateway reaches this Mac (Colima)
GATEWAY_URL="${WSO2_GATEWAY_URL:-http://default-default.gateway.localhost:19080}"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

for entry in catalog:8101 reservations:8102 payments:8103 concierge:8104; do
  name="${entry%%:*}" port="${entry##*:}"
  (cd "$ROOT" && exec "$PYTHON" -m "servers.${name}_server" --port "$port" \
      --allowed-host "$GATEWAY_HOST" --public-url "$GATEWAY_URL/staywise-$name/mcp") \
      >"$LOG_DIR/$name.log" 2>&1 &
  pids+=("$!")
  echo "started $name on http://127.0.0.1:$port/mcp (log: data/logs/$name.log)"
done

# macOS ships bash 3.2 (no `wait -n`), so poll until any server exits.
while :; do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "a server exited; see $LOG_DIR" >&2
      exit 1
    fi
  done
  sleep 2
done
