#!/usr/bin/env bash
# Expose the three local MCP servers on public HTTPS URLs with Cloudflare quick tunnels, so the
# Agent Manager console (which rejects private upstream addresses) can register them.
#
# Usage:
#   scripts/mcp-tunnels.sh start     # one tunnel per server; prints the public /mcp URLs
#   scripts/mcp-tunnels.sh status    # show the URLs of running tunnels
#   scripts/mcp-tunnels.sh stop      # close every tunnel
#
# The servers have NO authentication: anyone with a URL can call every tool (the data is made up).
# Quick-tunnel URLs are random, change on every start, and come with no uptime guarantee. Stop the
# tunnels when the demo is over.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/.run/tunnels"
SERVERS=("hotel-info:8101" "reservations:8102" "unity-loyalty:8103")
URL_WAIT_SECONDS=30

log() { printf '[tunnels] %s\n' "$*" >&2; }
die() { printf '[tunnels] ERROR: %s\n' "$*" >&2; exit 1; }

# tunnel_url <name>: the trycloudflare URL a running tunnel logged, or nothing.
tunnel_url() {
  grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN_DIR/$1.log" 2>/dev/null | head -n 1 || true
}

is_running() {
  local pid_file="$RUN_DIR/$1.pid"
  [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null
}

start() {
  command -v cloudflared >/dev/null 2>&1 || die "cloudflared not found; install it with: brew install cloudflared"
  mkdir -p "$RUN_DIR"
  local entry name port
  for entry in "${SERVERS[@]}"; do
    name="${entry%%:*}"
    port="${entry##*:}"
    if is_running "$name"; then
      log "$name: tunnel already running"
      continue
    fi
    curl -s -o /dev/null -m 3 "http://localhost:$port/mcp" ||
      die "$name is not listening on localhost:$port; start it with: python -m servers.${name//-/_}_server"
    # The Host rewrite keeps the server's DNS-rebinding protection on: it only accepts localhost names.
    nohup cloudflared tunnel --no-autoupdate --url "http://localhost:$port" \
      --http-host-header "localhost:$port" >"$RUN_DIR/$name.log" 2>&1 &
    echo $! >"$RUN_DIR/$name.pid"
    log "$name: starting tunnel to localhost:$port (pid $!)"
  done
  for entry in "${SERVERS[@]}"; do
    name="${entry%%:*}"
    local waited=0
    until [[ -n "$(tunnel_url "$name")" ]]; do
      is_running "$name" || die "$name: cloudflared exited; see $RUN_DIR/$name.log"
      ((waited >= URL_WAIT_SECONDS)) && die "$name: no tunnel URL after ${URL_WAIT_SECONDS}s; see $RUN_DIR/$name.log"
      sleep 1
      waited=$((waited + 1))
    done
  done
  status
}

status() {
  local entry name url
  for entry in "${SERVERS[@]}"; do
    name="${entry%%:*}"
    url="$(tunnel_url "$name")"
    if is_running "$name" && [[ -n "$url" ]]; then
      printf '%-14s %s/mcp\n' "$name" "$url"
    else
      printf '%-14s (not running)\n' "$name"
    fi
  done
}

stop() {
  local entry name
  for entry in "${SERVERS[@]}"; do
    name="${entry%%:*}"
    if is_running "$name"; then
      kill "$(cat "$RUN_DIR/$name.pid")"
      log "$name: tunnel stopped"
    fi
    rm -f "$RUN_DIR/$name.pid"
  done
}

case "${1:-}" in
  start) start ;;
  status) status ;;
  stop) stop ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
