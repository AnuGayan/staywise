# Hard Rock Concierge

A chat concierge for **Hard Rock Hotel Miami**. It checks a guest's bookings, books rooms and reads their **Unity by Hard Rock** points balance. It has three parts:

- **UI** (`hardrock-concierge-ui`, port 9100): signs the guest in with the **ThunderID bundled in WSO2 Agent Manager** and sends their chat, with their token, to the agent.
- **Agent** (`hardrock-concierge --serve`, port 8000): an API only. It runs an Ollama model over 9 tools from three MCP servers. The **Agent Manager gateway validates the user token**, and the agent never does. The agent reads the token to know which guest it serves, and exchanges it at ThunderID for a narrow token per tool call.
- **Three MCP servers**: no authentication of their own. WSO2 gateway MCP proxies add authentication (mcp-auth) and per-tool authorization (mcp-authz) in front of them.

The design is in [`docs/concierge-flow.html`](docs/concierge-flow.html). All hotel, guest, booking and points data is made up for the demo.

```
          sign in (code + PKCE)          token exchange: user token + agent token → 1-scope token
 guest ⇄ UI :9100 ───────────▶ ThunderID ◀──────────────────────────────┐
          │ Bearer <user token>                                          │
          ▼                                                              │
   Agent Manager gateway (validates) ──▶ agent :8000 ──▶ Ollama qwen3:1.7b
                                          │ Bearer <1-scope token> + API-Key
                                          ▼
                       gateway MCP proxies ──▶ hotel-info :8101 · reservations :8102 · unity-loyalty :8103
```

## Tools

| Server | Tool | Effect | Scope (exchanged per call; enforced by mcp-authz) |
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

Tools that touch guest data take a `member_id`. The agent takes it from the forwarded user token: its `username` claim is mapped to a Unity member id by [`config/members.json`](config/members.json), and the agent pins that id in its system prompt. The terminal chat has no token and uses `--member-id` (default `HR-100245`).

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

The UI is a confidential OIDC client of the Agent Manager ThunderID (`http://default-idp.amp.localhost:8080`). Register it, with the three demo guests:

```bash
./wso2/thunder/thunder-setup.sh --from-cluster     # needs kubectl access to the amp-local k3d cluster, jq
```

The script reads the Agent Manager system client secret from the cluster, imports [`wso2/thunder/hardrock-resources.yaml`](wso2/thunder/hardrock-resources.yaml) and writes `.env.thunder` (mode 600, git-ignored). It keeps any lines in that file it doesn't manage. It creates:

- the resource server `urn:hardrock:concierge-api` with scopes `hotel:read`, `bookings:read`, `bookings:write`, `unity:read`
- the password login flow `hardrock-password-login`
- the app `hardrock-concierge-web` for the UI (authorization code + PKCE, callback `http://localhost:9100/auth/callback` and the `127.0.0.1` variant)
- users `alex`, `priya`, `daniel` sharing one generated password (`DEMO_PASSWORD` in `.env.thunder`)
- the role `hardrock-guest`, granting those users all four scopes

Re-running updates in place and keeps the secrets. `./wso2/thunder/thunder-teardown.sh --from-cluster` removes everything. To serve the UI from another URL, set `AGENT_PUBLIC_URLS` before running setup and `CONCIERGE_PUBLIC_URL` before starting the UI.

## Run

Each server runs in its own terminal. The reservations and Unity servers create and seed `data/*.db` on first start. To reset them, delete the files and restart.

```bash
python -m servers.hotel_info_server       # http://127.0.0.1:8101/mcp
python -m servers.reservations_server     # http://127.0.0.1:8102/mcp  (--db / RESERVATIONS_DB_PATH)
python -m servers.unity_server            # http://127.0.0.1:8103/mcp  (--db / UNITY_DB_PATH)
```

Flags shared by all three: `--host`, `--port`, `--transport {streamable-http,stdio}`, `--allowed-host NAME` (repeatable; adds a Host header the server accepts, e.g. the address a gateway or cluster uses to reach it).

Then the agent and the UI:

```bash
hardrock-concierge --serve                          # agent API on http://127.0.0.1:8000
set -a; source .env.thunder; set +a
hardrock-concierge-ui                               # UI on http://localhost:9100, talking to the local agent
AGENT_URL=http://default-default.am-gateway.localhost:19080/<agent> hardrock-concierge-ui   # or the hosted agent

hardrock-concierge                                  # terminal chat (no sign-in)
hardrock-concierge -p "What's my Unity points balance?" --member-id HR-100377 -v
```

Open http://localhost:9100. Every sign-in asks for credentials (`prompt=login`), so **Sign out** lets you switch guests. Locally there's no gateway, so nothing validates the token the agent receives. Run it that way only on your own machine.

**UI** (`hardrock-concierge-ui`). It redirects to ThunderID with PKCE (S256), `nonce`, `state` and `resource=urn:hardrock:concierge-api`. On the callback it redeems the code with its client secret and verifies the ID token (signature against Thunder's JWKS, issuer, audience, expiry, nonce). It then keeps the access token server-side. The browser gets only an opaque HttpOnly, SameSite=Lax session cookie. Each chat message goes to `$AGENT_URL/chat` with `Authorization: Bearer <user token>` (plus `AGENT_API_KEY` in `AGENT_API_KEY_HEADER`, default `X-API-Key`, if set). If the agent or gateway answers 401, the UI ends the session and asks the guest to sign in again.

| UI route | |
|---|---|
| `GET /` | chat page; redirects to `/login` when signed out |
| `GET /login`, `GET /auth/callback`, `POST /logout` | sign-in and sign-out |
| `GET /me` | the signed-in user |
| `POST /chat {"message"}`, `DELETE /chat` | chat as the signed-in guest; start a new conversation |
| `GET /health` | where the agent is |

**Agent** (`hardrock-concierge --serve`). Serves `POST /chat {"message", "conversation_id"?}` and `GET /health`. It reads the user token from `Authorization: Bearer …`, or from `X-Forwarded-Authorization`, without validating it. It answers 401 if there's no token and 403 if the user has no Unity membership. Conversations are keyed by the token's `sub` plus `conversation_id`, so one guest can't continue another's.

**MCP tokens** (`concierge/tokens.py`). When Agent Manager provides the agent identity (`AMP_AGENTID_CLIENT_ID`, `AMP_AGENTID_CLIENT_SECRET`, `AMP_AGENTID_TOKEN_ENDPOINT`), every tool call works like this:

1. The agent gets its own token (`client_credentials`, `resource=urn:hardrock:concierge-api`).
2. It exchanges the guest's token (subject) plus its own token (actor) for a token with only that tool's scope (RFC 8693).
3. It sends that token as `Authorization: Bearer` to the MCP proxy, next to the `API-Key`.

Tokens are cached per guest and scope until a minute before they expire. If ThunderID refuses, or doesn't grant the scope, the tool returns `Error: not authorized to use <tool>: …` and the model explains it. Without the identity variables (e.g. locally), MCP calls carry only their API keys.

**MCP servers from environment variables** (what the image uses in Agent Manager): with `--mcp-from-env` (or `MCP_FROM_ENV=true`) the agent ignores `--config` and reads, for each prefix in `MCP_ENV_PREFIXES` (default `HOTEL_INFO,RESERVATIONS,UNITY_LOYALTY`):

| Variable | Meaning |
|---|---|
| `<PREFIX>_URL` | the MCP endpoint, or several separated by commas |
| `<PREFIX>_API_KEY` | sent as the `API-Key` header (with an empty `Authorization` header, which a per-call token replaces), as in Agent Manager's client snippet |

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

The image runs only the agent API, on port 8000 (`PORT`), the port Agent Manager gives the agent's service. It needs no sign-in settings, because the gateway validates user tokens. Agent Manager injects what the agent uses:

- **MCP servers:** `<PREFIX>_URL` / `<PREFIX>_API_KEY`. The image sets `MCP_FROM_ENV=true`.
- **Model:** `LLM_BASE_URL`, `LLM_API_KEY`. The image sets `LLM_API_KEY_HEADER=X-API-Key`.
- **Agent identity for token exchange:** `AMP_AGENTID_*`. `AMP_AGENTID_TOKEN_ENDPOINT` is Thunder's in-cluster service on port 8090, which the agent sandbox's network policy allows.
- **Tracing:** `AMP_OTEL_ENDPOINT`, `AMP_AGENT_API_KEY`.

Run the UI on your machine with `AGENT_URL` set to the agent's endpoint, e.g. `http://default-default.am-gateway.localhost:19080/<agent>`. While Agent Manager still requires an API key on that endpoint, set `AGENT_API_KEY` too.

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
