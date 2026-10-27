#!/usr/bin/env bash
# Create (or remove) the load-test guests loadtest01..loadtestNN in the Agent Manager ThunderID.
#
# Usage:
#   ./wso2/thunder/loadtest-users.sh --from-cluster              # create/update 50 users
#   ./wso2/thunder/loadtest-users.sh --from-cluster --count 10   # fewer
#   ./wso2/thunder/loadtest-users.sh --from-cluster --batch-size 2   # smaller imports if 5 still time out
#   ./wso2/thunder/loadtest-users.sh --from-cluster --remove     # delete them and their role
#
# Each user gets the role hardrock-loadtest-guest (the same scopes as hardrock-guest) and maps to its own
# Unity member HR-3000NN (servers/demo_data.py, config/members.json). They share one generated password,
# stored as LOADTEST_PASSWORD in .env.thunder (reused on re-runs). Run thunder-setup.sh first: the users
# sign in through its app and resource server. Re-running updates in place (fixed ids).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

ENV_FILE="${THUNDER_ENV_FILE:-$(cd "$SCRIPT_DIR/../.." && pwd)/.env.thunder}"
RESOURCE_SERVER_ID="019a5cb2-0000-7000-8000-000000000010"
ROLE_ID="019a5cb2-0000-7000-8000-000000000070"
ROLE_NAME="hardrock-loadtest-guest"
MAX_USERS=99
BATCH_SIZE=5

FROM_CLUSTER=0
REMOVE=0
COUNT=50
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-cluster) FROM_CLUSTER=1; shift ;;
    --remove) REMOVE=1; shift ;;
    --count) COUNT="${2:-}"; shift 2 ;;
    --batch-size) BATCH_SIZE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
[[ "$COUNT" =~ ^[0-9]+$ ]] && ((COUNT >= 1 && COUNT <= MAX_USERS)) || die "--count must be 1..$MAX_USERS"
[[ "$BATCH_SIZE" =~ ^[0-9]+$ ]] && ((BATCH_SIZE >= 1)) || die "--batch-size must be a positive number"

require_tools curl jq
log "THUNDER_BASE=$THUNDER_BASE"
resolve_admin_credentials "$FROM_CLUSTER"
get_admin_token

user_id() { printf '019a5cb2-0000-7000-8000-%012d' $((100 + $1)); }
username() { printf 'loadtest%02d' "$1"; }

if [[ "$REMOVE" == "1" ]]; then
  api DELETE "/roles/$ROLE_ID"
  case "$API_STATUS" in 200|202|204) log "deleted role $ROLE_NAME" ;; 404) log "already gone: role $ROLE_NAME" ;;
    *) die "DELETE role -> HTTP $API_STATUS" ;; esac
  for ((i = 1; i <= MAX_USERS; i++)); do
    api DELETE "/users/$(user_id "$i")"
    case "$API_STATUS" in 200|202|204) log "deleted $(username "$i")" ;; 404) ;;
      *) die "DELETE $(username "$i") -> HTTP $API_STATUS" ;; esac
  done
  log "done."
  exit 0
fi

prev_password=""
[[ -f "$ENV_FILE" ]] && prev_password=$(sed -n "s/^LOADTEST_PASSWORD *= *//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^'//" -e "s/'\$//")
password="${LOADTEST_PASSWORD:-${prev_password:-Lt-$(openssl rand -hex 8)-9a}}"
[[ "$password" != *[\"\'\\]* ]] || die "LOADTEST_PASSWORD must not contain quotes or backslashes"

# Save the password before importing anything, so a failed or timed-out run never leaves users whose
# password nobody knows. Kept next to the other demo secrets (mode 600), replacing an earlier value.
touch "$ENV_FILE" && chmod 600 "$ENV_FILE"
grep -v -E "^LOADTEST_PASSWORD *=" "$ENV_FILE" >"$ENV_FILE.tmp" || true
printf "LOADTEST_PASSWORD='%s'\n" "$password" >>"$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"
log "LOADTEST_PASSWORD saved in $ENV_FILE"

user_yaml() {
  local i="$1" name
  name=$(username "$i")
  cat <<YAML
resource_type: user
id: $(user_id "$i")
type: Person
ouHandle: default
attributes:
  username: $name
  email: $name@example.com
  name: Load Test $(printf '%02d' "$i")
  given_name: Load
  family_name: Test $(printf '%02d' "$i")
credentials:
  password: "{{.LOADTEST_PASSWORD}}"
YAML
}

# import_batch <label> <yaml>: POST /import (upsert), retrying once: a gateway timeout (504) can hit while
# ThunderID is still working, and re-importing the same ids is safe.
import_batch() {
  local label="$1" content="$2" body attempt failed
  body=$(jq -n --arg content "$content" --arg LOADTEST_PASSWORD "$password" \
    '{content: $content, variables: {LOADTEST_PASSWORD: $LOADTEST_PASSWORD}, options: {upsert: true, continueOnError: true}}')
  for attempt in 1 2; do
    api POST /import "$body"
    [[ "$API_STATUS" == "200" ]] && break
    log "$label: HTTP $API_STATUS (attempt $attempt)"
  done
  [[ "$API_STATUS" == "200" ]] ||
    die "$label: import failed: HTTP $API_STATUS $(jq -c '{code,message,description}' <<<"$API_BODY" 2>/dev/null || true); re-run the script (it updates in place) or lower --batch-size"
  failed=$(jq -r '.summary.failed' <<<"$API_BODY")
  if [[ "$failed" != "0" ]]; then
    jq -c '.results[] | select(.status != "success") | {resourceType, resourceName, error: (.error // .message // .errorDescription)}' <<<"$API_BODY" >&2
    die "$label: $failed resource(s) failed to import"
  fi
  log "$label: $(jq -r '[.results[] | .operation // "-"] | group_by(.) | map("\(.[0])=\(length)") | join(", ")' <<<"$API_BODY")"
}

# Users first, a few per request: each user's password hash takes time, and the gateway in front of
# ThunderID ends requests after ~30 s, so one 50-user import times out (HTTP 504).
for ((first = 1; first <= COUNT; first += BATCH_SIZE)); do
  last=$((first + BATCH_SIZE - 1 < COUNT ? first + BATCH_SIZE - 1 : COUNT))
  # "---" only between documents: a trailing one makes an empty document, which the importer rejects
  # ("document N root must be a YAML mapping").
  content=""
  for ((i = first; i <= last; i++)); do content+="${content:+---$'\n'}$(user_yaml "$i")"$'\n'; done
  import_batch "users $(username "$first")..$(username "$last")" "$content"
done

assignments=""
for ((i = 1; i <= COUNT; i++)); do
  assignments+="  - id: $(user_id "$i")
    type: user
"
done
import_batch "role $ROLE_NAME" "resource_type: role
id: $ROLE_ID
name: $ROLE_NAME
description: Hard Rock Miami concierge load-test guests (same scopes as hardrock-guest)
ouHandle: default
permissions:
  - resourceServerId: $RESOURCE_SERVER_ID
    permissions:
      - hotel:read
      - bookings:read
      - bookings:write
      - unity:read
assignments:
$assignments"
log "done. Users $(username 1)..$(username "$COUNT") -> Unity members HR-300001..HR-$((300000 + COUNT))"
