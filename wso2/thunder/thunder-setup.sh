#!/usr/bin/env bash
# Configure the Agent Manager ThunderID (1.0.0) for the Hard Rock concierge. Idempotent: safe to re-run.
#
# Usage:
#   ./wso2/thunder/thunder-setup.sh --from-cluster      # reads the AMP system client secret via kubectl
#   THUNDER_ADMIN_CLIENT_ID=... THUNDER_ADMIN_CLIENT_SECRET=... ./wso2/thunder/thunder-setup.sh
#
# Environment (all optional):
#   THUNDER_BASE            ThunderID base URL (default http://default-idp.amp.localhost:8080)
#   THUNDER_ADMIN_RESOURCE  resource indicator for the admin token, if the default probing fails
#   THUNDER_ENV_FILE        output file (default: <repo>/.env.thunder)
#   AGENT_PUBLIC_URLS       where browsers reach the concierge, comma-separated
#                           (default http://localhost:9100,http://127.0.0.1:9100)
#   WEB_CLIENT_SECRET / DEMO_PASSWORD   generated if unset
#
# Secrets are reused from an existing THUNDER_ENV_FILE when present, so re-runs keep the same
# credentials. Nothing secret is printed; the demo password is in THUNDER_ENV_FILE (mode 600).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

RESOURCES_FILE="$SCRIPT_DIR/hardrock-resources.yaml"
ENV_FILE="${THUNDER_ENV_FILE:-$(cd "$SCRIPT_DIR/../.." && pwd)/.env.thunder}"
THUNDER_RESOURCE="urn:hardrock:concierge-api"
WEB_CLIENT_ID="hardrock-concierge-web"
ID_RS="019a5cb2-0000-7000-8000-000000000010"
ID_APP="019a5cb2-0000-7000-8000-000000000040"

FROM_CLUSTER=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-cluster) FROM_CLUSTER=1; shift ;;
    -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

require_tools curl jq sed awk

# prev_value <key>: a value from the existing env file, without its single quotes.
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

# pick <env value> <key> <fallback>: explicit env value, else the previous run's value, else fallback.
pick() {
  local env_value="$1" key="$2" fallback="$3" prev
  if [[ -n "$env_value" ]]; then printf '%s' "$env_value"; return; fi
  prev=$(prev_value "$key")
  if [[ -n "$prev" ]]; then printf '%s' "$prev"; return; fi
  printf '%s' "$fallback"
}

AGENT_PUBLIC_URLS="${AGENT_PUBLIC_URLS:-http://localhost:9100,http://127.0.0.1:9100}"
WEB_REDIRECT_URIS=""
IFS=',' read -ra public_urls <<<"$AGENT_PUBLIC_URLS"
for url in "${public_urls[@]}"; do
  url="${url// /}"
  url="${url%/}"
  [[ -n "$url" ]] && WEB_REDIRECT_URIS+="${WEB_REDIRECT_URIS:+,}$url/auth/callback"
done
[[ -n "$WEB_REDIRECT_URIS" ]] || die "AGENT_PUBLIC_URLS gave no redirect URIs"

WEB_CLIENT_SECRET=$(pick "${WEB_CLIENT_SECRET:-}" CONCIERGE_CLIENT_SECRET "$(gen_secret)")
DEMO_PASSWORD=$(pick "${DEMO_PASSWORD:-}" DEMO_PASSWORD "Hr-$(gen_secret | cut -c1-16)-9a")
for var in WEB_CLIENT_SECRET DEMO_PASSWORD WEB_REDIRECT_URIS; do
  [[ "${!var}" != *[\"\'\\]* && "${!var}" != *$'\n'* ]] || die "$var must not contain quotes, backslashes or newlines"
done

log "THUNDER_BASE=$THUNDER_BASE"
resolve_admin_credentials "$FROM_CLUSTER"
get_admin_token

discovery=$(curl -sS "$THUNDER_BASE/.well-known/openid-configuration") ||
  die "cannot fetch $THUNDER_BASE/.well-known/openid-configuration"
THUNDER_ISSUER=$(jq -r '.issuer // empty' <<<"$discovery")
[[ -n "$THUNDER_ISSUER" ]] || die "discovery document has no issuer"

content=$(awk -v web="$WEB_REDIRECT_URIS" '
  $0 == "#WEB_REDIRECT_URIS#" {
    n = split(web, parts, ",")
    for (i = 1; i <= n; i++) print "        - \"" parts[i] "\""
    next
  }
  { print }' "$RESOURCES_FILE")

import_body=$(jq -n --arg content "$content" \
  --arg WEB_CLIENT_ID "$WEB_CLIENT_ID" --arg WEB_CLIENT_SECRET "$WEB_CLIENT_SECRET" \
  --arg DEMO_PASSWORD "$DEMO_PASSWORD" \
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

api GET "/resource-servers/$ID_RS"
[[ "$API_STATUS" == "200" ]] || die "resource server $ID_RS not readable after import (HTTP $API_STATUS)"
log "resource server: $(jq -r '.identifier' <<<"$API_BODY")"
api GET "/applications/$ID_APP"
[[ "$API_STATUS" == "200" ]] || die "application $ID_APP not readable after import (HTTP $API_STATUS)"
log "application: $(jq -r '.name' <<<"$API_BODY")"

write_kv() { printf "%s='%s'\n" "$1" "$2"; }
MANAGED_KEYS='THUNDER_ISSUER|THUNDER_RESOURCE|CONCIERGE_CLIENT_ID|CONCIERGE_CLIENT_SECRET|CONCIERGE_PUBLIC_URL|DEMO_USERS|DEMO_PASSWORD'
# Lines this script doesn't manage (e.g. keys you added by hand) are carried over unchanged.
kept_lines=""
if [[ -f "$ENV_FILE" ]]; then
  kept_lines=$(grep -v -E "^[[:space:]]*(#|($MANAGED_KEYS)[[:space:]]*=|$)" "$ENV_FILE" || true)
fi
umask 077
{
  echo "# Written by wso2/thunder/thunder-setup.sh. Holds secrets: do not commit."
  write_kv THUNDER_ISSUER "$THUNDER_ISSUER"
  write_kv THUNDER_RESOURCE "$THUNDER_RESOURCE"
  write_kv CONCIERGE_CLIENT_ID "$WEB_CLIENT_ID"
  write_kv CONCIERGE_CLIENT_SECRET "$WEB_CLIENT_SECRET"
  write_kv CONCIERGE_PUBLIC_URL "${public_urls[0]%/}"
  write_kv DEMO_USERS "alex,priya,daniel"
  write_kv DEMO_PASSWORD "$DEMO_PASSWORD"
  if [[ -n "$kept_lines" ]]; then
    printf '%s\n' "$kept_lines"
  fi
} >"$ENV_FILE"
log "wrote $ENV_FILE (client secret and demo password; mode 600)"
log "done. Start the concierge with:  set -a; source $ENV_FILE; set +a; python -m concierge --serve"
