#!/usr/bin/env bash
# Configure ThunderID (1.0.0) for the StayWise demo. Idempotent: safe to re-run.
#
# Usage:
#   THUNDER_ADMIN_CLIENT_ID=... THUNDER_ADMIN_CLIENT_SECRET=... ./thunder-setup.sh
#   ./thunder-setup.sh --from-cluster      # reads the AMP system client secret via kubectl
#
# Environment (all optional unless noted):
#   THUNDER_BASE            ThunderID base URL (default http://default-idp.amp.localhost:8080)
#   THUNDER_ADMIN_RESOURCE  resource indicator for the admin token, if the default probing fails
#   THUNDER_INSECURE=1      pass -k to curl (self-signed TLS)
#   THUNDER_ENV_FILE        output file (default: <full_flow>/.env.thunder)
#   MOCK_PHONE_URL          SMS gateway URL (default http://192.168.5.2:9100/mock-phone/sms)
#   MOCK_PHONE_KEY          value of the X-Mock-Phone-Key header (generated if unset)
#   DEMO_USER / DEMO_USER_PASSWORD / DEMO_USER_MOBILE / DEMO_USER_EMAIL
#   WEB_CLIENT_SECRET / STEPUP_CLIENT_SECRET / AGENT_CLIENT_SECRET (generated if unset)
#   AGENT_PUBLIC_URLS       where the agent is reached, comma-separated (default http://localhost:9100);
#                           e.g. add http://default-default.agentmanager.localhost:19080/staywise when hosted
#   WEB_REDIRECT_URI / STEPUP_REDIRECT_URI   comma-separated; default: derived from AGENT_PUBLIC_URLS
#
# Secrets are reused from an existing THUNDER_ENV_FILE when present, so re-runs keep the same
# credentials. Nothing secret is printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

RESOURCES_FILE="$SCRIPT_DIR/staywise-resources.yaml"
ENV_FILE="${THUNDER_ENV_FILE:-$(cd "$SCRIPT_DIR/../.." && pwd)/.env.thunder}"
THUNDER_RESOURCE="urn:staywise:hotel-api"
STEPUP_ACR_WANTED="mfa"

# Fixed ids from staywise-resources.yaml (used for post-import checks).
ID_RS="019a5ca1-0000-7000-8000-000000000010"
ID_USER="019a5ca1-0000-7000-8000-000000000030"
ID_AGENT="019a5ca1-0000-7000-8000-000000000050"

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

FROM_CLUSTER=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-cluster) FROM_CLUSTER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_tools curl jq sed

# prev_value <KEY>: value of KEY from an existing env file (parsed, never sourced).
prev_value() {
  [[ -f "$ENV_FILE" ]] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^'//" -e "s/'\$//"
}

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 48
  fi
}

# pick <ENV_VALUE> <KEY> <generator-default>: env > previous env file > default.
pick() {
  local env_value="$1" key="$2" fallback="$3" prev
  if [[ -n "$env_value" ]]; then printf '%s' "$env_value"; return; fi
  prev=$(prev_value "$key")
  if [[ -n "$prev" ]]; then printf '%s' "$prev"; return; fi
  printf '%s' "$fallback"
}

# --- inputs ---------------------------------------------------------------------------------
WEB_CLIENT_ID="staywise-web"
STEPUP_CLIENT_ID="staywise-stepup"
AGENT_CLIENT_ID="staywise-concierge-agent"
AGENT_PUBLIC_URLS="${AGENT_PUBLIC_URLS:-http://localhost:9100}"
# redirect_uris <suffix>: <url><suffix> for each AGENT_PUBLIC_URLS entry, comma-separated.
redirect_uris() {
  local suffix="$1" out="" url urls=()
  IFS=',' read -ra urls <<<"$AGENT_PUBLIC_URLS"
  for url in "${urls[@]}"; do
    url="${url// /}"
    url="${url%/}"
    [[ -n "$url" ]] && out+="${out:+,}$url$suffix"
  done
  printf '%s' "$out"
}
WEB_REDIRECT_URI="${WEB_REDIRECT_URI:-$(redirect_uris /auth/callback)}"
STEPUP_REDIRECT_URI="${STEPUP_REDIRECT_URI:-$(redirect_uris /auth/stepup/callback)}"
[[ -n "$WEB_REDIRECT_URI" && -n "$STEPUP_REDIRECT_URI" ]] || die "AGENT_PUBLIC_URLS gave no redirect URIs"
MOCK_PHONE_URL="${MOCK_PHONE_URL:-http://192.168.5.2:9100/mock-phone/sms}"
DEMO_USER="${DEMO_USER:-guest}"
DEMO_USER_EMAIL="${DEMO_USER_EMAIL:-guest@staywise.example}"
DEMO_USER_MOBILE="${DEMO_USER_MOBILE:-+94770000000}"

WEB_CLIENT_SECRET=$(pick "${WEB_CLIENT_SECRET:-}" WEB_CLIENT_SECRET "$(gen_secret)")
STEPUP_CLIENT_SECRET=$(pick "${STEPUP_CLIENT_SECRET:-}" STEPUP_CLIENT_SECRET "$(gen_secret)")
AGENT_CLIENT_SECRET=$(pick "${AGENT_CLIENT_SECRET:-}" AGENT_CLIENT_SECRET "$(gen_secret)")
MOCK_PHONE_KEY=$(pick "${MOCK_PHONE_KEY:-}" MOCK_PHONE_KEY "$(gen_secret)")
DEMO_USER_PASSWORD=$(pick "${DEMO_USER_PASSWORD:-}" DEMO_USER_PASSWORD "Sw-$(gen_secret | cut -c1-16)-9a")

# The custom SMS client splits the header string on "," and ":"; keep the key plain.
[[ "$MOCK_PHONE_KEY" =~ ^[A-Za-z0-9._~-]+$ ]] || die "MOCK_PHONE_KEY must match [A-Za-z0-9._~-]+"
# Values are substituted into double-quoted YAML strings and single-quoted env-file lines.
for var in MOCK_PHONE_URL DEMO_USER DEMO_USER_EMAIL DEMO_USER_MOBILE DEMO_USER_PASSWORD \
  WEB_CLIENT_SECRET STEPUP_CLIENT_SECRET AGENT_CLIENT_SECRET WEB_REDIRECT_URI STEPUP_REDIRECT_URI; do
  [[ "${!var}" != *[\"\'\\]* && "${!var}" != *$'\n'* ]] || die "$var must not contain quotes, backslashes or newlines"
done

# --- admin token ----------------------------------------------------------------------------
log "THUNDER_BASE=$THUNDER_BASE"
resolve_admin_credentials "$FROM_CLUSTER"
get_admin_token

# --- does this deployment know the "mfa" auth class? ------------------------------------------
# The step-up flow's first prompt maps its only action to auth class "mfa", so a completed step-up
# always carries ID token acr="mfa". Registering acrValues=[mfa] on the app additionally makes
# ThunderID honor/enforce acr_values=mfa, but an app's acrValues must be listed in the server's
# oauth.auth_class.acr_amr (deployment.yaml) or the app is rejected. Detect that via discovery.
discovery=$(curl -sS ${CURL_TLS_OPTS[@]+"${CURL_TLS_OPTS[@]}"} "$THUNDER_BASE/.well-known/openid-configuration") ||
  die "cannot fetch $THUNDER_BASE/.well-known/openid-configuration"
THUNDER_ISSUER=$(jq -r '.issuer // empty' <<<"$discovery")
STEPUP_ACR="$STEPUP_ACR_WANTED"
if jq -e --arg a "$STEPUP_ACR_WANTED" '(.acr_values_supported // []) | index($a)' >/dev/null <<<"$discovery"; then
  APP_ACR="$STEPUP_ACR_WANTED"
  log "auth class '$APP_ACR' is registered on the server: staywise-stepup gets acrValues=[$APP_ACR]"
else
  APP_ACR=""
  log "NOTE: server does not advertise acr '$STEPUP_ACR_WANTED' (oauth.auth_class.acr_amr not set);"
  log "      staywise-stepup is created without acrValues. acr_values=$STEPUP_ACR_WANTED is then ignored"
  log "      (not rejected), the flow still enforces password + SMS OTP and the ID token still has acr=$STEPUP_ACR_WANTED."
fi

# --- import ---------------------------------------------------------------------------------
# Redirect URIs are lists (local and hosted agent), so they are expanded here rather than as variables.
content=$(awk -v acr="$APP_ACR" -v web="$WEB_REDIRECT_URI" -v stepup="$STEPUP_REDIRECT_URI" '
  function uris(list,   n, i, parts) {
    n = split(list, parts, ",")
    for (i = 1; i <= n; i++) { gsub(/ /, "", parts[i]); if (parts[i] != "") print "        - \"" parts[i] "\"" }
  }
  $0 == "#STEPUP_ACR_BLOCK#" { if (acr != "") { print "      acrValues:"; print "        - " acr } next }
  $0 == "#WEB_REDIRECT_URIS#" { uris(web); next }
  $0 == "#STEPUP_REDIRECT_URIS#" { uris(stepup); next }
  { print }' "$RESOURCES_FILE")

import_body=$(jq -n --arg content "$content" \
  --arg MOCK_PHONE_URL "$MOCK_PHONE_URL" --arg MOCK_PHONE_KEY "$MOCK_PHONE_KEY" \
  --arg DEMO_USER "$DEMO_USER" --arg DEMO_USER_EMAIL "$DEMO_USER_EMAIL" \
  --arg DEMO_USER_MOBILE "$DEMO_USER_MOBILE" --arg DEMO_USER_PASSWORD "$DEMO_USER_PASSWORD" \
  --arg WEB_CLIENT_ID "$WEB_CLIENT_ID" --arg WEB_CLIENT_SECRET "$WEB_CLIENT_SECRET" \
  --arg STEPUP_CLIENT_ID "$STEPUP_CLIENT_ID" --arg STEPUP_CLIENT_SECRET "$STEPUP_CLIENT_SECRET" \
  --arg AGENT_CLIENT_ID "$AGENT_CLIENT_ID" --arg AGENT_CLIENT_SECRET "$AGENT_CLIENT_SECRET" \
  '{content: $content,
    variables: ($ARGS.named | del(.content)),
    options: {upsert: true, continueOnError: true}}')

log "importing $(basename "$RESOURCES_FILE") ..."
api POST /import "$import_body"
import_resp="$API_BODY"
[[ "$API_STATUS" == "200" ]] || die "import failed: HTTP $API_STATUS $(jq -c '{code,description}' <<<"$import_resp" 2>/dev/null || true)"
jq -r '.results[] | "[thunder]   \(.status)\t\(.operation // "-")\t\(.resourceType)\t\(.resourceName // .resourceId)"' <<<"$import_resp" >&2
failed=$(jq -r '.summary.failed' <<<"$import_resp")
if [[ "$failed" != "0" ]]; then
  jq -c '.results[] | select(.status != "success") | {resourceType, resourceName, resourceId, error: (.error // .message // .errorDescription)}' <<<"$import_resp" >&2
  die "$failed resource(s) failed to import"
fi

# --- post-import checks ---------------------------------------------------------------------
api GET "/resource-servers/$ID_RS"
rs_json="$API_BODY"
[[ "$API_STATUS" == "200" ]] || die "resource server $ID_RS not readable after import (HTTP $API_STATUS)"
api GET "/users/$ID_USER"
user_json="$API_BODY"
[[ "$API_STATUS" == "200" ]] || die "user $ID_USER not readable after import (HTTP $API_STATUS)"
api GET "/agents/$ID_AGENT"
agent_json="$API_BODY"
[[ "$API_STATUS" == "200" ]] || die "agent $ID_AGENT not readable after import (HTTP $API_STATUS)"
log "resource server: $(jq -r '.identifier' <<<"$rs_json")"

# Smoke test: the agent's own client_credentials token must carry ops:notify for the resource.
token_request "$AGENT_CLIENT_ID" "$AGENT_CLIENT_SECRET" grant_type=client_credentials \
  "resource=$THUNDER_RESOURCE" scope=ops:notify
cc="$TOKEN_BODY"
if [[ "$TOKEN_HTTP_STATUS" == "200" ]] && jq -e '.scope | split(" ") | index("ops:notify")' >/dev/null <<<"$cc"; then
  log "smoke test: agent client_credentials -> scope '$(jq -r .scope <<<"$cc")' OK"
else
  die "smoke test failed: agent client_credentials -> HTTP $TOKEN_HTTP_STATUS $(jq -c '{error,error_description}' <<<"$cc" 2>/dev/null || true)"
fi

# --- write the env file ---------------------------------------------------------------------
write_kv() { printf "%s='%s'\n" "$1" "$2"; }
tmp_env=$(umask 077 && mktemp "${ENV_FILE}.XXXXXX")
{
  echo "# Generated by wso2/thunder/thunder-setup.sh -- contains secrets, do not commit."
  write_kv THUNDER_BASE "$THUNDER_BASE"
  write_kv THUNDER_ISSUER "${THUNDER_ISSUER:-$THUNDER_BASE}"
  write_kv THUNDER_RESOURCE "$THUNDER_RESOURCE"
  write_kv WEB_CLIENT_ID "$WEB_CLIENT_ID"
  write_kv WEB_CLIENT_SECRET "$WEB_CLIENT_SECRET"
  write_kv WEB_REDIRECT_URI "$WEB_REDIRECT_URI"
  write_kv STEPUP_CLIENT_ID "$STEPUP_CLIENT_ID"
  write_kv STEPUP_CLIENT_SECRET "$STEPUP_CLIENT_SECRET"
  write_kv STEPUP_REDIRECT_URI "$STEPUP_REDIRECT_URI"
  write_kv STEPUP_ACR "$STEPUP_ACR"
  write_kv STEPUP_ACR_ENFORCED "$([[ -n "$APP_ACR" ]] && echo true || echo false)"
  write_kv AGENT_CLIENT_ID "$AGENT_CLIENT_ID"
  write_kv AGENT_CLIENT_SECRET "$AGENT_CLIENT_SECRET"
  write_kv AGENT_ID "$(jq -r '.id' <<<"$agent_json")"
  write_kv MOCK_PHONE_URL "$MOCK_PHONE_URL"
  write_kv MOCK_PHONE_KEY "$MOCK_PHONE_KEY"
  write_kv DEMO_USER "$DEMO_USER"
  write_kv DEMO_USER_PASSWORD "$DEMO_USER_PASSWORD"
  write_kv DEMO_USER_ID "$(jq -r '.id' <<<"$user_json")"
  write_kv DEMO_USER_MOBILE "$DEMO_USER_MOBILE"
} >"$tmp_env"
mv -f "$tmp_env" "$ENV_FILE"
log "wrote $ENV_FILE (mode 600)"
log "done."
