# Hard Rock Concierge

A chat concierge for **Hard Rock Hotel Miami**. It checks a guest's bookings, books rooms and reads their **Unity by Hard Rock** points balance. The agent runs an Ollama model and uses 9 tools from three standalone MCP servers. Guests sign in to the web chat with the **ThunderID bundled in WSO2 Agent Manager**.

The servers have **no authentication** in this phase. Later, WSO2 gateway MCP proxies will add authentication (mcp-auth) and per-tool authorization (mcp-authz) in front of them without changing server code. The design, with diagrams of both phases, is in [`docs/concierge-flow.html`](docs/concierge-flow.html).

All hotel, guest, booking and points data is made up for the demo.

```
 ThunderID (Agent Manager) ◀── sign in: OIDC code + PKCE, server-side
        ▲
        │             ┌──── MCP 2025-06-18 over Streamable HTTP, no auth ────┐
 guest ⇄ concierge ───┼─▶ hotel-info     :8101/mcp  get_hotel_info, list_room_types
 (web chat :9100)     ├─▶ reservations   :8102/mcp  check_availability, list_bookings, get_booking,
        │             │                             create_booking, cancel_booking   (SQLite)
        ▼             └─▶ unity-loyalty  :8103/mcp  get_unity_balance, get_points_activity (SQLite)
   Ollama qwen3:1.7b
```

## Tools

| Server | Tool | Effect | Scope planned for the gateway phase |
|---|---|---|---|
| hotel-info | `get_hotel_info` | read | `hotel:read` |
| hotel-info | `list_room_types` | read | `hotel:read` |
| reservations | `check_availability` | read | `bookings:read` |
| reservations | `list_bookings` | read | `bookings:read` |
| reservations | `get_booking` | read | `bookings:read` |
| reservations | `create_booking` | write | `bookings:write` |
| reservations | `cancel_booking` | write | `bookings:write` |
| unity-loyalty | `get_unity_balance` | read | `unity:read` |
| unity-loyalty | `get_points_activity` | read | `unity:read` |

Tools that touch guest data take a `member_id`. In the web chat it comes from the signed-in guest: their ThunderID `username` is mapped to a Unity member id by [`config/members.json`](config/members.json), and the agent pins that id in its system prompt. The terminal chat has no sign-in and uses `--member-id` (default `HR-100245`).

The MCP servers still trust the `member_id` argument, so anything that reaches them directly can read any member's data. `get_booking` and `cancel_booking` only match bookings owned by the given member.

| ThunderID user | Unity member | Tier |
|---|---|---|
| `alex` | `HR-100245` Alex Morgan | Legend |
| `priya` | `HR-100377` Priya Natarajan | Star |
| `daniel` | `HR-100512` Daniel Okafor | Icon |

## Setup

Requires Python 3.11+ and [Ollama](https://ollama.com) with a tool-calling model.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ollama pull qwen3:1.7b
```

## Sign-in setup (once)

The web chat is a confidential OIDC client of the Agent Manager ThunderID (`http://default-idp.amp.localhost:8080`). Register it, with the three demo guests:

```bash
./wso2/thunder/thunder-setup.sh --from-cluster     # needs kubectl access to the amp-local k3d cluster, jq
```

The script reads the Agent Manager system client secret from the cluster, imports [`wso2/thunder/hardrock-resources.yaml`](wso2/thunder/hardrock-resources.yaml) and writes `.env.thunder` (mode 600, git-ignored). It creates:

- the resource server `urn:hardrock:concierge-api` with scopes `hotel:read`, `bookings:read`, `bookings:write`, `unity:read`
- the password login flow `hardrock-password-login`
- the app `hardrock-concierge-web` (authorization code + PKCE, callback `http://localhost:9100/auth/callback` and the `127.0.0.1` variant)
- users `alex`, `priya`, `daniel` sharing one generated password (`DEMO_PASSWORD` in `.env.thunder`)
- the role `hardrock-guest`, granting those users all four scopes

Re-running updates in place and keeps the secrets. `./wso2/thunder/thunder-teardown.sh --from-cluster` removes everything. To serve the chat from another URL, set `AGENT_PUBLIC_URLS` before running setup and `CONCIERGE_PUBLIC_URL` before starting the agent.

## Run

Each server runs in its own terminal. The reservations and Unity servers create and seed `data/*.db` on first start. To reset them, delete the files and restart.

```bash
python -m servers.hotel_info_server       # http://127.0.0.1:8101/mcp
python -m servers.reservations_server     # http://127.0.0.1:8102/mcp  (--db / RESERVATIONS_DB_PATH)
python -m servers.unity_server            # http://127.0.0.1:8103/mcp  (--db / UNITY_DB_PATH)
```

Flags shared by all three: `--host`, `--port`, `--transport {streamable-http,stdio}`, `--allowed-host NAME` (repeatable; adds a Host header the server accepts, e.g. the address a gateway or cluster uses to reach it).

Then start the agent:

```bash
set -a; source .env.thunder; set +a
python -m concierge --serve                       # web chat on http://localhost:9100 (sign-in required)
python -m concierge                               # terminal chat (no sign-in)
python -m concierge -p "What's my Unity points balance?"
python -m concierge --member-id HR-100377 -v       # another guest; show MCP connection details
```

Open http://localhost:9100. Every sign-in asks for credentials (`prompt=login`), so **Sign out** lets you switch guests.

How sign-in works: the agent redirects to ThunderID with PKCE (S256), a `nonce`, `state` and `resource=urn:hardrock:concierge-api`. On the callback it redeems the code with its client secret, verifies the ID token (signature against Thunder's JWKS, issuer, audience, expiry, nonce) and maps `username` to a member id. The browser only gets an opaque HttpOnly, SameSite=Lax session cookie, valid for the token lifetime (1 hour). Tokens never reach the browser.

| Route | |
|---|---|
| `GET /` | chat page; redirects to `/login` when signed out |
| `GET /login`, `GET /auth/callback`, `POST /logout` | sign-in and sign-out |
| `GET /me` | the signed-in guest and member id |
| `POST /chat {"message"}`, `DELETE /chat` | chat as the signed-in guest; start over |
| `GET /health` | model and tools (public) |

**MCP servers from environment variables** (what the image uses in Agent Manager): with `--mcp-from-env` (or `MCP_FROM_ENV=true`) the agent ignores `--config` and reads, for each prefix in `MCP_ENV_PREFIXES` (default `HOTEL_INFO,RESERVATIONS,UNITY_LOYALTY`):

| Variable | Meaning |
|---|---|
| `<PREFIX>_URL` | the MCP endpoint, or several separated by commas |
| `<PREFIX>_API_KEY` | sent as the `API-Key` header (with an empty `Authorization` header), as in Agent Manager's client snippet |

A prefix with neither variable set is skipped. A URL without a key, or a key without a URL, is an error. If your variables use other prefixes, set `MCP_ENV_PREFIXES` to match.

```bash
MCP_FROM_ENV=true UNITY_LOYALTY_URL=https://…/mcp UNITY_LOYALTY_API_KEY=… hardrock-concierge -p "What's my Unity points balance?"
```

Otherwise, server lists are in `mcp_servers*.json` (Claude Desktop `mcpServers` shape, `${VAR}` expanded from the environment):

| File | Use |
|---|---|
| `mcp_servers.json` | Local: `127.0.0.1:8101-8103` (default) |
| `mcp_servers.remote.json` | Servers on another host: `${MCP_HOST}` |
| `mcp_servers.gateway.json` | Through WSO2 gateway MCP proxies: `${WSO2_GATEWAY_URL}/{hotel-info,reservations,unity}/mcp` |

## Agent Manager image

```bash
docker build -t hardrock-concierge:dev .
```

The image serves the web chat on port 9100. It reads the MCP servers from Agent Manager's `<PREFIX>_URL` / `<PREFIX>_API_KEY` variables (`MCP_FROM_ENV=true`) and calls the model through the gateway LLM provider (`LLM_BASE_URL`, `LLM_API_KEY`, with `LLM_API_KEY_HEADER=X-API-Key`). It listens on port 8000 (`PORT`), the port Agent Manager gives the agent's service.

Sign-in settings to add to the agent:

| Variable | Value |
|---|---|
| `CONCIERGE_CLIENT_SECRET` (secret) | from `.env.thunder` |
| `CONCIERGE_PUBLIC_URL` | the agent's public URL, e.g. `http://default-default.am-gateway.localhost:19080/<agent>`. Its path becomes the prefix for every redirect. |

That URL must also be registered with ThunderID: re-run `thunder-setup.sh` with it added to `AGENT_PUBLIC_URLS`. `THUNDER_ISSUER` defaults to the browser-facing `http://default-idp.amp.localhost:8080`. The pod can't resolve that name, so the image sets `THUNDER_INTERNAL_URL` to Thunder's in-cluster service on port 8090, which the agent sandbox's network policy allows. Discovery, token and JWKS calls go there, and the issuer check is unchanged.

Agent Manager's gateway puts an API key in front of the agent's public URL, so a browser gets `401 Valid API key required` there. Either open the endpoint up in Agent Manager, or for a demo port-forward the service and use the already-registered `http://localhost:9100` (set `CONCIERGE_PUBLIC_URL` to that):

```bash
kubectl --kubeconfig <(k3d kubeconfig get amp-local) -n <agent namespace> port-forward svc/<agent> 9100:8000
```

Hosted pods can only reach the gateway, not the Mac directly, so both the model and the MCP servers go through it. The MCP servers are registered in the console from their public tunnel URLs (next section).

## Public MCP URLs for the Agent Manager console

The console won't register MCP servers on private addresses. `scripts/mcp-tunnels.sh` puts each local server behind a [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) (needs `cloudflared`; no account):

```bash
scripts/mcp-tunnels.sh start    # prints https://<random>.trycloudflare.com/mcp for each server
scripts/mcp-tunnels.sh status
scripts/mcp-tunnels.sh stop
```

Register each printed URL as an MCP server in the console. Each tunnel rewrites `Host` to `localhost:<port>`, so the servers keep their DNS-rebinding protection without extra `--allowed-host` flags.

- **The servers have no authentication.** Anyone who learns a URL can call every tool, including `create_booking` and `cancel_booking`. The data is made up, but stop the tunnels when you're done.
- The URLs are random and **change every time the tunnels start**, so re-register them in the console after a restart. For stable URLs, use a named Cloudflare tunnel on your own domain, or an ngrok static domain.
- Quick tunnels have no uptime guarantee. They're fine for a demo, not for anything longer-lived.

## Tracing (Agent Manager)

Docker-built agents bring their own instrumentation. The image installs the `amp` extra (`amp-instrumentation==0.4.1`) and starts with `amp-instrument hardrock-concierge ...`. A startup hook initialises Traceloop, which exports OTLP/HTTP spans to `AMP_OTEL_ENDPOINT` with the `x-amp-api-key: $AMP_AGENT_API_KEY` header. Agent Manager injects both variables into the pod. Without them the hook prints an error and the agent runs untraced.

Each guest turn is one trace:

```
hardrock_concierge.agent            one per question (concierge/agent.py)
├── ollama.chat                     each model call
├── get_unity_balance.tool          each MCP tool call
└── ollama.chat
```

The MCP `initialize` / `tools/list` calls at start-up appear as their own short traces. Prompts and tool results are recorded in the spans. Set `AMP_TRACE_CONTENT=false` in the deployment to leave them out.

To try it locally against any OTLP/HTTP collector:

```bash
pip install -e ".[amp]"
AMP_OTEL_ENDPOINT=http://127.0.0.1:4318 AMP_AGENT_API_KEY=local amp-instrument hardrock-concierge -p "What's my Unity points balance?"
```

## Develop

```bash
ruff format --check . && ruff check .
pytest -q
```
