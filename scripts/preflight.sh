#!/usr/bin/env bash
# Pre-demo checks. Prints OK/FAIL per item and exits non-zero if anything failed.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/.env.thunder" ] && set -a && . "$ROOT/.env.thunder" && set +a
THUNDER="${THUNDER_BASE:-http://default-idp.amp.localhost:8080}"
GATEWAY="${WSO2_GATEWAY_URL:-http://default-default.gateway.localhost:19080}"
AGENT="${PUBLIC_URL:-http://localhost:9100}"
failed=0

check() { # name, command...
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "OK    $name"; else echo "FAIL  $name"; failed=1; fi
}
status_is() { # expected, curl args...
  local expected="$1"; shift
  [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$@")" = "$expected" ]
}
mcp_list() { # url → expect 401 without a token (proves server + guard/gateway are up)
  status_is 401 -X POST "$1" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
}

check ".env.thunder present" test -f "$ROOT/.env.thunder"
check "Thunder discovery ($THUNDER)" status_is 200 "$THUNDER/.well-known/openid-configuration"
check "Thunder offers token exchange" sh -c "curl -s -m 5 '$THUNDER/.well-known/openid-configuration' | grep -q token-exchange"
for entry in catalog:8101 reservations:8102 payments:8103 concierge:8104; do
  name="${entry%%:*}" port="${entry##*:}"
  check "MCP $name direct (401 without token)" mcp_list "http://127.0.0.1:$port/mcp"
  check "MCP $name via gateway (401 without token)" mcp_list "$GATEWAY/staywise-$name/mcp"
done
check "Ollama reachable" status_is 200 "${OLLAMA_HOST:-http://127.0.0.1:11434}/api/tags"
check "Agent backend ($AGENT)" status_is 200 "$AGENT/health"
check "Web UI built" test -f "$ROOT/web/dist/index.html"

exit "$failed"
