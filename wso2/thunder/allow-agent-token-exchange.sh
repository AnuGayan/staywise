#!/usr/bin/env bash
# Allow an Agent Manager agent identity to use the token-exchange grant in ThunderID, so the agent can
# swap a guest's token for a per-tool MCP token (RFC 8693). Agent Manager creates the identity with
# client_credentials only.
#
# Usage:
#   ./wso2/thunder/allow-agent-token-exchange.sh --from-cluster <client-id>            # show the change
#   ./wso2/thunder/allow-agent-token-exchange.sh --from-cluster <client-id> --apply    # make it
#   ./wso2/thunder/allow-agent-token-exchange.sh --from-cluster <client-id> --revert   # take it back
#
# <client-id> is the agent's AMP_AGENTID_CLIENT_ID. The update re-sends the agent as read (ThunderID's
# PUT /agents/{id} replaces the whole agent) with only the grant list changed. It leaves the client
# secret out, which ThunderID 1.0.0 treats as "keep the existing secret" for secret-based clients.
# Agent Manager may reset the grant if it re-provisions the identity; re-run with --apply if so.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

GRANT="urn:ietf:params:oauth:grant-type:token-exchange"
FROM_CLUSTER=0
MODE=show
CLIENT_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-cluster) FROM_CLUSTER=1; shift ;;
    --apply) MODE=apply; shift ;;
    --revert) MODE=revert; shift ;;
    -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown argument: $1 (see --help)" ;;
    *) [[ -z "$CLIENT_ID" ]] || die "only one client id expected"; CLIENT_ID="$1"; shift ;;
  esac
done
[[ -n "$CLIENT_ID" ]] || die "pass the agent's client id (AMP_AGENTID_CLIENT_ID); see --help"

require_tools curl jq
log "THUNDER_BASE=$THUNDER_BASE"
resolve_admin_credentials "$FROM_CLUSTER"
get_admin_token

# grants_of <agent json>: the oauth2 grant types, space-separated.
grants_of() {
  jq -r '[.inboundAuthConfig[]? | select(.type == "oauth2") | .config.grantTypes[]?] | join(" ")' <<<"$1"
}

api GET "/agents?limit=200"
[[ "$API_STATUS" == "200" ]] || die "GET /agents -> HTTP $API_STATUS"
agent_ids=$(jq -r '(if type == "array" then . else (.agents // []) end)[] | .id' <<<"$API_BODY")
agent_id=""
agent_json=""
while read -r id; do
  [[ -n "$id" ]] || continue
  api GET "/agents/$id"
  [[ "$API_STATUS" == "200" ]] || continue
  if jq -e --arg c "$CLIENT_ID" '[.inboundAuthConfig[]? | select(.type == "oauth2") | .config.clientId] | index($c)' \
    >/dev/null <<<"$API_BODY"; then
    agent_id="$id"
    agent_json="$API_BODY"
    break
  fi
done <<<"$agent_ids"
[[ -n "$agent_id" ]] || die "no agent with OAuth client id $CLIENT_ID"
log "agent: $(jq -r '.name' <<<"$agent_json") ($agent_id)"
log "grant types now: $(grants_of "$agent_json")"

has_grant=$(jq --arg g "$GRANT" '[.inboundAuthConfig[]? | select(.type == "oauth2") | .config.grantTypes[]?] | index($g) != null' <<<"$agent_json")
case "$MODE" in
  show)
    if [[ "$has_grant" == "true" ]]; then log "token exchange is already allowed"; else log "would add: $GRANT (re-run with --apply)"; fi
    exit 0 ;;
  apply)
    [[ "$has_grant" == "false" ]] || { log "token exchange is already allowed; nothing to do"; exit 0; }
    edit='(.config.grantTypes // []) + [$g] | unique' ;;
  revert)
    [[ "$has_grant" == "true" ]] || { log "token exchange is not allowed; nothing to do"; exit 0; }
    edit='(.config.grantTypes // []) - [$g]' ;;
esac

body=$(jq --arg g "$GRANT" "del(.id) | .inboundAuthConfig |= map(if .type == \"oauth2\" then (.config.grantTypes = ($edit) | del(.config.clientSecret)) else . end)" <<<"$agent_json")
api PUT "/agents/$agent_id" "$body"
[[ "$API_STATUS" == "200" ]] || die "PUT /agents/$agent_id -> HTTP $API_STATUS $(jq -c '{code,message,description}' <<<"$API_BODY" 2>/dev/null || true)"

api GET "/agents/$agent_id"
[[ "$API_STATUS" == "200" ]] || die "re-reading the agent failed: HTTP $API_STATUS"
after_client=$(jq -r '[.inboundAuthConfig[]? | select(.type == "oauth2") | .config.clientId][0]' <<<"$API_BODY")
[[ "$after_client" == "$CLIENT_ID" ]] || die "client id changed to $after_client; check the agent in ThunderID"
log "grant types now: $(grants_of "$API_BODY")"

# The secret must still work: Agent Manager's copy is the only one. client_credentials proves it.
if [[ -n "${AGENT_CLIENT_SECRET:-}" ]]; then
  token_request "$CLIENT_ID" "$AGENT_CLIENT_SECRET" grant_type=client_credentials "resource=${THUNDER_RESOURCE:-urn:hardrock:concierge-api}"
  [[ "$TOKEN_HTTP_STATUS" == "200" ]] && log "agent client secret still works" ||
    die "client_credentials with the agent secret -> HTTP $TOKEN_HTTP_STATUS $(jq -c '{error,error_description}' <<<"$TOKEN_BODY" 2>/dev/null || true)"
else
  log "set AGENT_CLIENT_SECRET to also check the agent's secret still works"
fi
log "done."
