#!/usr/bin/env bash
# Turn Moesif analytics on (or off) for the Agent Manager API Platform gateway (default-default).
#
# Usage:
#   MOESIF_APPLICATION_ID=... ./wso2/gateway/enable-moesif.sh     # enable / update the collector id
#   ./wso2/gateway/enable-moesif.sh --disable                     # remove it again
#
# What it changes (namespace default-default, cluster amp-local):
#   - Secret gateway-moesif with APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID
#   - ConfigMap api-platform-default-default-gw-gateway-config: an [analytics] section whose
#     application_id is an {{ env }} token, so the id never sits in the ConfigMap
#   - the gateway controller and runtime Deployments: env from that Secret (this restarts them)
#
# It patches the Helm-managed objects in place instead of `helm upgrade`, because Helm 4 re-renders
# this chart's integer settings (e.g. onfailurestatuscode) as floats. A later `helm upgrade` of the
# gateway release will drop these changes; re-run this script afterwards.
set -euo pipefail

NAMESPACE="${GATEWAY_NAMESPACE:-default-default}"
CLUSTER="${AMP_K3D_CLUSTER:-amp-local}"
CONFIGMAP="api-platform-default-default-gw-gateway-config"
DEPLOYMENTS=(
  "api-platform-default-default-gw-gateway-controller:gateway-controller"
  "api-platform-default-default-gw-gateway-gateway-runtime:gateway-runtime"
)
SECRET="gateway-moesif"
ENV_NAME="APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID"
BEGIN_MARK="# >>> moesif analytics (wso2/gateway/enable-moesif.sh)"
END_MARK="# <<< moesif analytics"

log() { printf '[moesif] %s\n' "$*" >&2; }
die() { printf '[moesif] ERROR: %s\n' "$*" >&2; exit 1; }

MODE=enable
case "${1:-}" in
  "") ;;
  --disable) MODE=disable ;;
  -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) die "unknown argument: $1 (see --help)" ;;
esac

for tool in kubectl jq python3; do command -v "$tool" >/dev/null 2>&1 || die "$tool not found"; done
kc() {
  if [[ -n "${KUBECONFIG:-}" ]] || ! command -v k3d >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" "$@"
  else
    kubectl --kubeconfig <(k3d kubeconfig get "$CLUSTER") -n "$NAMESPACE" "$@"
  fi
}

# config_toml_with_block <current config.toml> <enable|disable>: the config with our block replaced or removed.
config_toml_with_block() {
  python3 - "$1" "$2" "$BEGIN_MARK" "$END_MARK" "$ENV_NAME" <<'PY'
import re
import sys

current, mode, begin, end, env_name = sys.argv[1:]
config = re.sub(rf"\n*{re.escape(begin)}.*?{re.escape(end)}\n*", "\n", open(current).read(), flags=re.S)
config = config.rstrip("\n") + "\n"
if mode == "enable":
    if re.search(r"^\s*\[analytics\]", config, flags=re.M):
        sys.exit("config.toml already has an [analytics] section that this script did not write; edit it by hand")
    config = config.rstrip("\n") + f"""

{begin}
[analytics]
enabled = true
enabled_publishers = ["moesif"]
[analytics.publishers.moesif]
application_id = '{{{{ env "{env_name}" "" }}}}'
moesif_base_url = "https://api.moesif.net"
publish_interval = 5
event_queue_size = 10000
batch_size = 50
timer_wakeup_seconds = 3
{end}
"""
sys.stdout.write(config)
PY
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
kc get configmap "$CONFIGMAP" -o json >"$work/cm.json" || die "cannot read ConfigMap $CONFIGMAP"
jq -r '.data["config.toml"]' "$work/cm.json" >"$work/config.toml"

if [[ "$MODE" == "enable" ]]; then
  [[ -n "${MOESIF_APPLICATION_ID:-}" ]] || die "set MOESIF_APPLICATION_ID to the Moesif collector application id"
  # The id goes in through a file, never on a command line (where `ps` would show it).
  printf '%s=%s\n' "$ENV_NAME" "$MOESIF_APPLICATION_ID" >"$work/secret.env"
  kc create secret generic "$SECRET" --from-env-file="$work/secret.env" --dry-run=client -o yaml | kc apply -f - >/dev/null
  log "secret $SECRET updated"
fi

config_toml_with_block "$work/config.toml" "$MODE" >"$work/config.new.toml"
if cmp -s "$work/config.toml" "$work/config.new.toml"; then
  log "config.toml already up to date"
else
  jq --rawfile toml "$work/config.new.toml" '.data["config.toml"] = $toml' "$work/cm.json" | kc replace -f - >/dev/null
  log "config.toml: analytics block $([[ "$MODE" == enable ]] && echo written || echo removed)"
fi

for entry in "${DEPLOYMENTS[@]}"; do
  deployment="${entry%%:*}"
  container="${entry##*:}"
  if [[ "$MODE" == "enable" ]]; then
    kc set env "deployment/$deployment" -c "$container" --from="secret/$SECRET" >/dev/null
  else
    kc set env "deployment/$deployment" -c "$container" "$ENV_NAME-" >/dev/null
  fi
  # set env only rolls the pods when the env changed; a config-only change needs an explicit restart.
  kc rollout restart "deployment/$deployment" >/dev/null
  kc rollout status "deployment/$deployment" --timeout=180s >/dev/null || die "$deployment did not become ready"
  log "$deployment restarted"
done

if [[ "$MODE" == "disable" ]]; then
  kc delete secret "$SECRET" --ignore-not-found >/dev/null
  log "secret $SECRET deleted"
fi
log "done. Check: kubectl -n $NAMESPACE logs deploy/${DEPLOYMENTS[1]%%:*} | grep -i moesif"
