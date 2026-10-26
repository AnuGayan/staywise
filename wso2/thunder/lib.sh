#!/usr/bin/env bash
# Shared helpers for thunder-setup.sh / thunder-teardown.sh. Sourced, not executed.
#
# Secrets are never placed on a command line (they would show in `ps`): credentials are passed
# to curl through a config file on stdin, and nothing secret is printed.

THUNDER_BASE="${THUNDER_BASE:-http://default-idp.amp.localhost:8080}"
THUNDER_BASE="${THUNDER_BASE%/}"

AMP_SYSTEM_CLIENT_ID="amp-system-client"
AMP_SECRET_NAMESPACE="amp-thunder-default-default"
AMP_SECRET_NAME="amp-thunder-default-default-system-client"
AMP_SECRET_KEY="client-secret"
# k3d cluster running Agent Manager; used for kubectl unless KUBECONFIG is set.
AMP_K3D_CLUSTER="${AMP_K3D_CLUSTER:-amp-local}"

# Identifier of the bootstrap "System" resource server in ThunderID 1.0.0. The `system` scope
# lives on it; when no defaultResourceServer is configured, client_credentials needs it as
# `resource`.
THUNDER_SYSTEM_RESOURCE_DEFAULT="https://localhost:8090/mcp"

CURL_TLS_OPTS=()
if [[ "${THUNDER_INSECURE:-0}" == "1" ]]; then
  CURL_TLS_OPTS=(-k)
fi

log() { printf '[thunder] %s\n' "$*" >&2; }
die() { printf '[thunder] ERROR: %s\n' "$*" >&2; exit 1; }

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool '$tool' not found in PATH"
  done
}

# curl_cfg_quote <value>: escape a value for a curl config file double-quoted string.
curl_cfg_quote() {
  local v="$1"
  v="${v//\\/\\\\}"
  v="${v//\"/\\\"}"
  printf '%s' "$v"
}

# token_request <client_id> <client_secret> <form-arg>...: POST /oauth2/token with HTTP Basic
# auth supplied via curl config on stdin. Sets TOKEN_BODY and TOKEN_HTTP_STATUS (call it
# directly, not inside $(...), or the globals are lost in the subshell).
token_request() {
  local client_id="$1" client_secret="$2"
  shift 2
  local args=() kv out
  for kv in "$@"; do
    args+=(--data-urlencode "$kv")
  done
  out=$(printf 'user = "%s:%s"\n' "$(curl_cfg_quote "$client_id")" "$(curl_cfg_quote "$client_secret")" |
    curl -sS ${CURL_TLS_OPTS[@]+"${CURL_TLS_OPTS[@]}"} -K - -w '\n%{http_code}' -X POST "${args[@]}" "$THUNDER_BASE/oauth2/token") ||
    die "token request to $THUNDER_BASE/oauth2/token failed (is THUNDER_BASE reachable?)"
  TOKEN_HTTP_STATUS="${out##*$'\n'}"
  TOKEN_BODY="${out%$'\n'*}"
}

# amp_kubectl <args>...: kubectl against the Agent Manager cluster. Honors KUBECONFIG when set;
# otherwise uses k3d's kubeconfig for AMP_K3D_CLUSTER, so the default context does not matter.
amp_kubectl() {
  if [[ -n "${KUBECONFIG:-}" ]] || ! command -v k3d >/dev/null 2>&1; then
    kubectl "$@"
    return
  fi
  kubectl --kubeconfig <(k3d kubeconfig get "$AMP_K3D_CLUSTER") "$@"
}

# resolve_admin_credentials <from_cluster:0|1>: sets ADMIN_CLIENT_ID / ADMIN_CLIENT_SECRET.
resolve_admin_credentials() {
  local from_cluster="$1"
  if [[ -n "${THUNDER_ADMIN_CLIENT_ID:-}" && -n "${THUNDER_ADMIN_CLIENT_SECRET:-}" ]]; then
    ADMIN_CLIENT_ID="$THUNDER_ADMIN_CLIENT_ID"
    ADMIN_CLIENT_SECRET="$THUNDER_ADMIN_CLIENT_SECRET"
    log "admin credentials: from THUNDER_ADMIN_CLIENT_ID/THUNDER_ADMIN_CLIENT_SECRET"
    return
  fi
  [[ "$from_cluster" == "1" ]] ||
    die "set THUNDER_ADMIN_CLIENT_ID and THUNDER_ADMIN_CLIENT_SECRET, or pass --from-cluster"
  require_tools kubectl base64
  local encoded
  encoded=$(amp_kubectl get secret "$AMP_SECRET_NAME" -n "$AMP_SECRET_NAMESPACE" \
    -o "jsonpath={.data.$AMP_SECRET_KEY}") ||
    die "could not read secret $AMP_SECRET_NAMESPACE/$AMP_SECRET_NAME (k3d cluster '$AMP_K3D_CLUSTER' running? or set KUBECONFIG)"
  [[ -n "$encoded" ]] || die "secret $AMP_SECRET_NAMESPACE/$AMP_SECRET_NAME has no key '$AMP_SECRET_KEY'"
  ADMIN_CLIENT_ID="$AMP_SYSTEM_CLIENT_ID"
  ADMIN_CLIENT_SECRET=$(printf '%s' "$encoded" | base64 --decode 2>/dev/null || printf '%s' "$encoded" | base64 -D)
  log "admin credentials: client '$ADMIN_CLIENT_ID' from cluster secret $AMP_SECRET_NAMESPACE/$AMP_SECRET_NAME"
}

# get_admin_token: sets ADMIN_TOKEN (client_credentials, scope=system). Tries, in order:
# THUNDER_ADMIN_RESOURCE (if set), no resource (works when defaultResourceServer is configured),
# then the bootstrap System resource server identifier.
get_admin_token() {
  local candidates=() resource body
  if [[ -n "${THUNDER_ADMIN_RESOURCE:-}" ]]; then
    candidates+=("$THUNDER_ADMIN_RESOURCE")
  fi
  candidates+=("" "$THUNDER_SYSTEM_RESOURCE_DEFAULT")
  for resource in "${candidates[@]}"; do
    if [[ -n "$resource" ]]; then
      token_request "$ADMIN_CLIENT_ID" "$ADMIN_CLIENT_SECRET" \
        grant_type=client_credentials scope=system "resource=$resource"
    else
      token_request "$ADMIN_CLIENT_ID" "$ADMIN_CLIENT_SECRET" grant_type=client_credentials scope=system
    fi
    body="$TOKEN_BODY"
    if [[ "$TOKEN_HTTP_STATUS" == "200" ]] && jq -e '.scope | split(" ") | index("system")' >/dev/null <<<"$body"; then
      ADMIN_TOKEN=$(jq -r .access_token <<<"$body")
      log "admin token acquired (resource='${resource:-<none>}')"
      return
    fi
    log "admin token attempt (resource='${resource:-<none>}') -> HTTP $TOKEN_HTTP_STATUS $(jq -c '{error,error_description}' <<<"$body" 2>/dev/null || true)"
  done
  die "could not obtain an admin token with scope 'system'"
}

# api <METHOD> <path> [json-body]: admin REST call. Sets API_BODY and API_STATUS (call it
# directly, not inside $(...)).
api() {
  local method="$1" path="$2" data="${3:-}" out
  local args=(-sS ${CURL_TLS_OPTS[@]+"${CURL_TLS_OPTS[@]}"} -K - -w '\n%{http_code}' -X "$method" -H 'Accept: application/json')
  if [[ -n "$data" ]]; then
    # stdin carries the auth header config, so the body goes through file descriptor 3.
    args+=(-H 'Content-Type: application/json' --data-binary @/dev/fd/3)
  fi
  out=$(printf 'header = "Authorization: Bearer %s"\n' "$ADMIN_TOKEN" |
    curl "${args[@]}" "$THUNDER_BASE$path" 3<<<"$data") ||
    die "$method $path failed (network)"
  API_STATUS="${out##*$'\n'}"
  API_BODY="${out%$'\n'*}"
}
