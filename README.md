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

Server lists are in `mcp_servers*.json` (Claude Desktop `mcpServers` shape, `${VAR}` expanded from the environment):

| File | Use |
|---|---|
| `mcp_servers.json` | Local: `127.0.0.1:8101-8103` (default) |
| `mcp_servers.remote.json` | Servers on another host: `${MCP_HOST}` |
| `mcp_servers.gateway.json` | Through WSO2 gateway MCP proxies: `${WSO2_GATEWAY_URL}/{hotel-info,reservations,unity}/mcp` |

## Agent Manager image

```bash
docker build -t hardrock-concierge:dev .
```

The image serves the web chat on port 9100 and reads `mcp_servers.remote.json` with `MCP_HOST=192.168.5.2`, the Mac host as seen from the k3d cluster. The deployment also needs the sign-in variables from `.env.thunder`, with `CONCIERGE_PUBLIC_URL` set to the URL browsers use and that URL registered through `AGENT_PUBLIC_URLS`. The pod must be able to reach ThunderID at `THUNDER_ISSUER`. This hasn't been tried in the cluster yet. For a pod to reach the servers and Ollama on the Mac:

- Start each server with `--host 0.0.0.0 --allowed-host 192.168.5.2`. This exposes the unauthenticated servers on your network.
- Run Ollama listening beyond loopback (`OLLAMA_HOST=0.0.0.0 ollama serve`), or set `OLLAMA_HOST` / `LLM_BASE_URL` in the deployment to another endpoint.

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
