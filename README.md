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
                                          │ Bearer <1-scope token>
                                          ▼
           gateway MCP proxies (JWT) ──▶ hotel-info :8101 · reservations :8102 · unity-loyalty :8103
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
hardrock-concierge-ui                               # UI on http://localhost:9100, talking to the hosted agent (melody)
AGENT_URL=http://localhost:8000 hardrock-concierge-ui   # or to the local agent

hardrock-concierge                                  # terminal chat (no sign-in)
hardrock-concierge -p "What's my Unity points balance?" --member-id HR-100377 -v
```

Open http://localhost:9100. Every sign-in asks for credentials (`prompt=login`), so **Sign out** lets you switch guests. Locally there's no gateway, so nothing validates the token the agent receives. Run it that way only on your own machine.

**UI** (`hardrock-concierge-ui`). It redirects to ThunderID with PKCE (S256), `nonce`, `state` and `resource=urn:hardrock:concierge-api`. On the callback it redeems the code with its client secret and verifies the ID token (signature against Thunder's JWKS, issuer, audience, expiry, nonce). It then keeps the access token server-side. The browser gets only an opaque HttpOnly, SameSite=Lax session cookie. Each chat message goes to `$AGENT_URL/chat` with `Authorization: Bearer <user token>` (plus `AGENT_API_KEY` in `AGENT_API_KEY_HEADER`, default `API-Key`, if set). If the agent or gateway answers 401, the UI ends the session and asks the guest to sign in again.

| UI route | |
|---|---|
| `GET /` | chat page; redirects to `/login` when signed out |
| `GET /login`, `GET /auth/callback`, `POST /logout` | sign-in and sign-out |
| `GET /me` | the signed-in user |
| `POST /chat {"message"}`, `DELETE /chat` | chat as the signed-in guest; start a new conversation |
| `GET /health` | where the agent is |

**Agent** (`hardrock-concierge --serve`). Serves `POST /chat {"message", "conversation_id"?}` and `GET /health`. It reads the user token from `Authorization: Bearer …`, or from `X-Forwarded-Authorization`, without validating it. It answers 401 if there's no token and 403 if the user has no Unity membership. Conversations are keyed by the token's `sub` plus `conversation_id`, so one guest can't continue another's.

**MCP tokens** (`concierge/tokens.py`). When Agent Manager provides the agent identity (`AMP_AGENTID_CLIENT_ID`, `AMP_AGENTID_CLIENT_SECRET`, `AMP_AGENTID_TOKEN_ENDPOINT`), every tool call works like this:

1. The agent gets its own token (`client_credentials`, `resource=urn:hardrock:concierge-api`). It also sends this token on the startup handshake (`initialize`, `tools/list`), before any guest is involved.
2. It exchanges the guest's token (subject) plus its own token (actor) for a token with only that tool's scope (RFC 8693).
3. It sends that token as `Authorization: Bearer` to the MCP proxy, whose JWT authentication validates it. An `API-Key` header is added only if `<PREFIX>_API_KEY` is set.

A tool outside the scope map, or a call without a guest token, carries the agent's own token. If the agent's token can't be obtained at startup, `--serve` exits with code 1.

Tokens are cached per guest and scope until a minute before they expire. If ThunderID refuses, or doesn't grant the scope, the tool returns `Error: not authorized to use <tool>: …` and the model explains it. Without the identity variables (e.g. locally), MCP calls carry no token.

**MCP servers from environment variables** (what the image uses in Agent Manager): with `--mcp-from-env` (or `MCP_FROM_ENV=true`) the agent ignores `--config` and reads, for each prefix in `MCP_ENV_PREFIXES` (default `HOTEL_INFO,RESERVATIONS,UNITY_LOYALTY`):

| Variable | Meaning |
|---|---|
| `<PREFIX>_URL` | the MCP endpoint, or several separated by commas |
| `<PREFIX>_API_KEY` | optional; sent as the `API-Key` header for proxies that authenticate with API keys. JWT-protected proxies need none: every request carries `Authorization: Bearer <token>` |

A prefix without a URL is skipped. A key without a URL is an error. If your variables use other prefixes, set `MCP_ENV_PREFIXES` to match.

```bash
MCP_FROM_ENV=true UNITY_LOYALTY_URL=https://…/mcp hardrock-concierge -p "What's my Unity points balance?"
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

- **MCP servers:** `<PREFIX>_URL`, plus `<PREFIX>_API_KEY` only for API-key proxies. The image sets `MCP_FROM_ENV=true`. With JWT-protected proxies, every MCP request carries a bearer token from the agent identity below.
- **Model:** `LLM_BASE_URL`, `LLM_API_KEY`. The image sets `LLM_API_KEY_HEADER=API-Key`, the header Agent Manager's LLM proxies check. A provider you configure yourself may use another, e.g. `X-API-Key`.
- **Agent identity for token exchange:** `AMP_AGENTID_*`. `AMP_AGENTID_TOKEN_ENDPOINT` is Thunder's in-cluster service on port 8090, which the agent sandbox's network policy allows.
- **Tracing:** `AMP_OTEL_ENDPOINT`, `AMP_AGENT_API_KEY`.

Run the UI on your machine. Its default `AGENT_URL` is the hosted agent's endpoint, `http://default-default.am-gateway.localhost:19080/melody`. While Agent Manager still requires an API key on that endpoint, set `AGENT_API_KEY` too.

Hosted pods can only reach the gateway, not the Mac directly, so both the model and the MCP servers go through it. The MCP servers are registered in the console from their public tunnel URLs (next section).

## End-to-end check

`hardrock-concierge-e2e` checks the deployed system the way a guest uses it, with no browser:

1. **Signs in** a demo guest at ThunderID: authorize with PKCE, then ThunderID's login flow API (`/flow/execute`) with the username and `DEMO_PASSWORD`, then `/oauth2/auth/callback`, then the code exchange. It uses the UI's app (`CONCIERGE_CLIENT_ID` / `CONCIERGE_CLIENT_SECRET`), so it gets the same user token the UI would.
2. **Checks the gateway**: a request with no token and one with a forged token must both be rejected.
3. **Plays a conversation** through the agent's gateway URL with the user token, one step per capability:
   - hotel info and room types
   - availability
   - booking a room, fetching it, listing bookings, then cancelling it
   - points balance and points activity

   Each step checks:
   - the expected tool was called
   - no tool returned an error, such as a refused token exchange
   - every call used the signed-in guest's member id
   - the result itself: dates, an `HRM-` confirmation number, the booking showing up, then `cancelled`

   A step whose tool the model skipped is retried once in a fresh conversation with a hint. The booking it makes is cancelled again; if not, the report says so.

```bash
hardrock-concierge-e2e                 # reads .env.thunder; signs in as alex; agent = UI default (melody)
hardrock-concierge-e2e --user priya -v # another guest; print every reply and tool call
```

It reads settings from the environment first, then `--env-file` (default `.env.thunder`, tolerating `KEY = 'value'` lines). `AGENT_URL` or `--agent-url` picks the agent. It exits 0 only if everything passed:

```
PASS sign in (code + PKCE)         0.7s  user token for alex
PASS no token is rejected          0.0s  HTTP 401
PASS forged token is rejected      0.0s  HTTP 401
PASS hotel info                    2.9s  get_hotel_info
...
PASS book a room                   7.1s  booked HRM-D8NHMR
...
PASS points balance                3.5s  18,450 points, Legend
12 passed, 0 failed, 0 skipped
```

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

## Moesif analytics (gateway)

`wso2/gateway/enable-moesif.sh` turns on the API Platform gateway's built-in Moesif publisher, so every request through the gateway is sent to Moesif: agent, MCP and LLM traffic alike.

```bash
MOESIF_APPLICATION_ID=<collector application id> ./wso2/gateway/enable-moesif.sh   # enable / change the id
./wso2/gateway/enable-moesif.sh --disable                                         # turn it off again
```

It changes three things in `default-default`:

- **Secret `gateway-moesif`** holding `APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID`.
- **ConfigMap `api-platform-default-default-gw-gateway-config`:** an `[analytics]` section with `enabled_publishers = ["moesif"]`. Its `application_id` is `{{ env "APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID" "" }}`, which the policy engine resolves at load time, so the id is never in the ConfigMap.
- **The gateway controller and runtime Deployments:** they take env from that Secret, and both are restarted.

The script never puts the collector id on a command line or in this repo.

Bodies and headers are not captured: `[collector]` stays at its defaults.

The objects belong to the Helm release `api-platform-default-default-gw`, but the script patches them in place rather than running `helm upgrade`. Helm 4 re-renders this chart's integer settings, such as `onfailurestatuscode = 401`, as floats. A later `helm upgrade` of that release will drop the analytics settings, so re-run the script afterwards.

Check it with:

```bash
kubectl --kubeconfig <(k3d kubeconfig get amp-local) -n default-default logs deploy/api-platform-default-default-gw-gateway-gateway-runtime | grep -i moesif
# "Moesif publisher added"; send errors show as "Moesif API request error"
```

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
