#!/usr/bin/env bash
# Remove everything thunder-setup.sh created in ThunderID. Safe to re-run (missing = skipped).
#
# Usage:
#   THUNDER_ADMIN_CLIENT_ID=... THUNDER_ADMIN_CLIENT_SECRET=... ./thunder-teardown.sh
#   ./thunder-teardown.sh --from-cluster
#   ./thunder-teardown.sh --remove-env-file   # also delete <full_flow>/.env.thunder
#
# Resources are matched by name/handle (falling back to nothing if absent), so this also cleans up
# copies created by hand with the contract names.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

ENV_FILE="${THUNDER_ENV_FILE:-$(cd "$SCRIPT_DIR/../.." && pwd)/.env.thunder}"
DEMO_USER="${DEMO_USER:-guest}"
RS_IDENTIFIER="urn:staywise:hotel-api"

FROM_CLUSTER=0
REMOVE_ENV=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-cluster) FROM_CLUSTER=1; shift ;;
    --remove-env-file) REMOVE_ENV=1; shift ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_tools curl jq

log "THUNDER_BASE=$THUNDER_BASE"
resolve_admin_credentials "$FROM_CLUSTER"
get_admin_token

# ids_matching <list-path> <array-key> <jq-filter>: ids of list items selected by the filter.
ids_matching() {
  api GET "$1"
  [[ "$API_STATUS" == "200" ]] || die "GET $1 -> HTTP $API_STATUS"
  jq -r --arg k "$2" "(if type == \"array\" then .[] else (.[\$k] // [])[] end) | select($3) | .id" <<<"$API_BODY"
}

# delete_path <path> <label>: DELETE, treating 404 as already gone.
delete_path() {
  api DELETE "$1"
  case "$API_STATUS" in
    200|202|204) log "deleted $2" ;;
    404) log "already gone: $2" ;;
    *) die "DELETE $1 ($2) -> HTTP $API_STATUS $(jq -c '{code,description}' <<<"$API_BODY" 2>/dev/null || true)" ;;
  esac
}

delete_all() {
  local list_path="$1" key="$2" filter="$3" item_path="$4" label="$5" id ids found=0
  ids=$(ids_matching "$list_path" "$key" "$filter")
  while read -r id; do
    [[ -n "$id" ]] || continue
    found=1
    delete_path "$item_path/$id" "$label ($id)"
  done <<<"$ids"
  [[ "$found" == "1" ]] || log "not found: $label"
}

# Dependents first: roles reference the user/agent/resource server, apps reference flows.
for role in staywise-guest staywise-agent; do
  delete_all "/roles?limit=100" roles ".name == \"$role\"" /roles "role $role"
done
for app in staywise-web staywise-stepup; do
  delete_all "/applications?limit=100" applications ".name == \"$app\"" /applications "application $app"
done
delete_all "/agents?limit=100" agents '.name == "staywise-concierge-agent"' /agents "agent staywise-concierge-agent"
delete_all "/users?filter=$(jq -rn --arg u "$DEMO_USER" '"username eq \"\($u)\"" | @uri')" users 'true' /users "user $DEMO_USER"
for handle in staywise-password-login staywise-stepup-mfa; do
  delete_all "/flows?limit=100" flows ".handle == \"$handle\"" /flows "flow $handle"
done
delete_all /connections/sms-gateway connections '.name == "staywise-mock-phone"' /connections/sms-gateway \
  "sms connection staywise-mock-phone"

# Resource server: actions, then resources, then the server itself.
rs_found=0
rs_ids=$(ids_matching "/resource-servers?limit=100" resourceServers ".identifier == \"$RS_IDENTIFIER\"")
while read -r rs_id; do
  [[ -n "$rs_id" ]] || continue
  rs_found=1
  res_ids=$(ids_matching "/resource-servers/$rs_id/resources?limit=100" resources 'true')
  while read -r res_id; do
    [[ -n "$res_id" ]] || continue
    delete_all "/resource-servers/$rs_id/resources/$res_id/actions?limit=100" actions 'true' \
      "/resource-servers/$rs_id/resources/$res_id/actions" "actions of resource $res_id"
    delete_path "/resource-servers/$rs_id/resources/$res_id" "resource $res_id"
  done <<<"$res_ids"
  delete_all "/resource-servers/$rs_id/actions?limit=100" actions 'true' \
    "/resource-servers/$rs_id/actions" "server-level actions"
  delete_path "/resource-servers/$rs_id" "resource server $RS_IDENTIFIER"
done <<<"$rs_ids"
[[ "$rs_found" == "1" ]] || log "not found: resource server $RS_IDENTIFIER"

if [[ "$REMOVE_ENV" == "1" && -f "$ENV_FILE" ]]; then
  rm -f "$ENV_FILE"
  log "removed $ENV_FILE"
fi
log "done."
