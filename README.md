# StayWise: a hotel concierge agent that steps up its own privileges

A demo of an AI agent that books hotels, takes payments and messages hotel staff, and gets
stronger OAuth permissions **only when a tool call needs them**:

- **Automatic step-up (delegated)**: the agent mints a narrower or stronger token itself by
  token exchange from the guest's login grant. The token's `act` claim names the agent.
- **Automatic step-up (agent identity)**: for hotel-operations tools the agent uses its own
  identity (client credentials), so the audit trail shows the agent, not the guest.
- **User step-up**: payments over $500, and cancelling a prepaid non-refundable stay, pause the
  tool call. The guest verifies with password + SMS OTP in a popup, and the call resumes.

Identity: **ThunderID 1.0.0** inside WSO2 Agent Manager. Enforcement: the **WSO2 AI gateway**
(`mcp-auth`, `mcp-authz`) **and** the MCP servers themselves.

```
 browser ── session cookie ──▶ StayWise backend (:9100)  ── tokens never leave it ──┐
   chat · security timeline        agent loop ─▶ LLM (Ollama, or via gateway)       │
   MFA popup · mock phone          StepUpTransport per MCP server ◀─ TokenBroker ◀──┤
        ▲                                │ Bearer <narrow token>            │       │
        │ SSE events                     ▼                                  ▼       │
        │                    WSO2 gateway :19080 (mcp-auth, mcp-authz)   ThunderID  │
        │                                │ 401 / 403 insufficient_scope   (default-idp)
        │                                ▼                                          │
        └── OTP SMS ◀── Thunder ── MCP servers :8101-8104 (guard: JWT + amount rules)
```

| Server | Tools | Needs |
|---|---|---|
| catalog :8101 | `search_hotels`, `get_room`, `check_availability` | `hotel:read` |
| reservations :8102 | `list_my_reservations` · `create_reservation`, `change_room`, `cancel_reservation` | `booking:read` · `booking:write` (cancelling a paid non-refundable stay: `payment:highvalue` from the step-up app) |
| payments :8103 | `get_loyalty_balance` · `charge_payment`, `redeem_points` | `booking:read` · `payment:charge` (≤ $500), `payment:highvalue` from the step-up app (> $500) |
| concierge :8104 | `notify_front_desk`, `request_housekeeping` | `ops:notify` (agent identity only) |

How each scope may be obtained is set in `config/stepup_policy.yaml`, never by the model.

## How the step-up works

1. At sign-in the guest consents once to `hotel:read booking:read booking:write payment:charge`
   (the *envelope*). That grant stays in the backend.
2. Each MCP server first gets a least-privilege token (`servers.*.baseline` in the policy),
   minted by token exchange with the agent's token as `actor_token`.
3. A gateway or server answers `403 … error="insufficient_scope", scope="booking:write"`.
   `StepUpTransport` catches it before the MCP SDK sees it, asks `TokenBroker` for a stronger
   token, and retries the same request:
   - `auto-obo` scopes are minted by token exchange. Thunder only narrows, so this works only
     inside the login envelope.
   - `auto-agent` scopes come from client credentials as the agent.
   - `user` scopes raise a `stepup_required` event. The UI opens the popup, which runs Thunder's
     step-up app with `acr_values=mfa` and `prompt=login`. The backend checks it is the same
     `sub` and that the ID token's `acr` is `mfa`, then resumes the call. If the guest declines
     or doesn't finish in time, the model gets a plain tool error and the MCP connection stays up.
4. Thunder 1.0.0 puts `acr` only in ID tokens (thunder-id/thunderid#5846). It also does not
   restrict an app to its configured scopes. So the servers require `payment:highvalue`
   **and** `client_id == STEPUP_CLIENT_ID`; only the MFA-only step-up app can satisfy that.
   Its tokens live for 5 minutes, and the agent reuses one within that window.

## Run it

Prerequisites:
- Agent Manager running (`amp-local` k3d on Colima).
- Python 3.11+.
- Node 20+.
- `jq`.
- Ollama with a tool-calling model. The default is `qwen3:1.7b`; a larger one (`-m qwen3:8b`) follows multi-step bookings more reliably.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
(cd web && npm install && npm run build)

# 1. Thunder: resource server, roles, apps, agent identity, MFA flow, SMS sender, demo user → .env.thunder
wso2/thunder/thunder-setup.sh --from-cluster          # reads the AM system client secret from k3d cluster amp-local
                                                      # (AMP_K3D_CLUSTER=<name> or KUBECONFIG=<file> to override)

# 2. MCP servers (reachable by the gateway at 192.168.5.2)
scripts/run-servers.sh

# 3. Gateway proxies (already applied once; re-apply after changes)
KUBECONFIG=<(k3d kubeconfig get amp-local) kubectl -n default-default apply -f wso2/mcp-proxies.k8s.yaml

# 4. The agent: tools through the gateway
.venv/bin/python -m staywise -v -c config/mcp_servers.wso2.json
#    or add  --llm-base-url "$WSO2_GATEWAY_URL/ollama" --llm-api-key-header X-API-Key  (LLM_API_KEY set)

scripts/preflight.sh       # every line should be OK
```

Open http://localhost:9100 and sign in as `guest`. The password is `DEMO_USER_PASSWORD` in
`.env.thunder`. The one-time codes appear on the mock phone panel.

## Host it in Agent Manager

The backend and its web UI run as a platform-hosted agent, built from this repo's `Dockerfile`. The
MCP servers stay on the Mac behind the gateway.

What the hosted-agent sandbox requires, and how StayWise handles it:

| Constraint | Handling |
|---|---|
| Served under `/<routePath>`, with the prefix stripped before the request reaches the app | The prefix comes from `PUBLIC_URL`'s path; the UI uses relative URLs |
| The external route ends every request after 30 s | The event stream is numbered; a reconnect with `Last-Event-ID` replays what was missed |
| Outbound traffic only to DNS, port 8090, the gateway (port 22893) and the public internet | Thunder back-channel goes to `THUNDER_INTERNAL_URL`; MCP and LLM traffic go through the gateway's in-cluster service |
| Runs as user 65534 on a read-only filesystem | The image writes nothing at runtime |
| Sessions are kept in memory | Keep 1 replica, with autoscaling off |

Steps:

1. **Ollama for the cluster.** Run `launchctl setenv OLLAMA_HOST 0.0.0.0` and restart Ollama. In the
   console, create an LLM provider for `http://192.168.5.2:11434` and an API key for it.
2. **MCP proxies.** Create them as in `wso2/mcp-proxies.k8s.yaml` (contexts, upstreams, `mcp-auth` with
   `forwardToken: true`, per-tool `mcp-authz`). If the console rejects the `192.168.5.2` upstreams, apply
   the file with kubectl as in [Run it](#run-it).
3. **Thunder, with both URLs.** Redirect URIs are registered for every listed URL:
   ```bash
   AGENT_PUBLIC_URLS=http://localhost:9100,http://default-default.agentmanager.localhost:19080/staywise \
     wso2/thunder/thunder-setup.sh --from-cluster
   ```
4. **Push this repo** to GitHub. `.env*` is git-ignored and excluded from the Docker build.
5. **Create the agent** in the console:
   - platform-hosted, custom API (not chat), Docker build, Dockerfile `./Dockerfile`, context `.`;
   - port `9100`, route path `staywise`;
   - environment from `wso2/agent-manager.env.example`, with the client secrets as secrets.

   `PUBLIC_URL` must match the endpoint URL the console shows. If it differs from the one in step 3,
   re-run step 3 with the shown URL.
6. **Point the SMS sender at the hosted mock phone.** After the first deploy, find the agent's service:
   ```bash
   KUBECONFIG=<(k3d kubeconfig get amp-local) kubectl get svc -A | grep staywise
   ```
   Then re-run step 3 with `MOCK_PHONE_URL=http://<service>.<namespace>.svc.cluster.local:9100/mock-phone/sms`.
7. **MCP servers.** Start them with `scripts/run-servers.sh`, or restart them so they load the new
   `.env.thunder`. Then open the console URL and sign in as `guest`.

To check the image builds before pushing, run `docker build -t staywise:local .`.

## Demo script

| # | Say | What the audience sees |
|---|---|---|
| 1 | "Find me a sea-view room in Galle for 14–16 November under $200" | Search runs with the baseline `hotel:read` token. The timeline card says *Existing token*. |
| 2 | "Book the Lighthouse Deluxe and pay the $90 deposit with my saved card" | `create_reservation` is refused (`booking:write`) and the agent steps up **by itself**. The badge says *Auto step-up · delegated*, and the claims show `act.sub` = the agent. The $90 charge does the same for `payment:charge`. |
| 3 | "Let the front desk know I'll arrive around 11pm" | The concierge call carries the **agent's own** token (`sub` = agent, no `act`). |
| 4 | "Upgrade me to the Ocean Suite and prepay the full $1,240 now" | `change_room` works. The charge pauses on *Extra verification needed*. In the popup the guest enters password + SMS OTP from the mock phone, and the charge completes, authorized by `staywise-stepup`. |
| 5 | Ask for another large charge, and press **Decline** | The tool returns "guest declined". The agent explains and offers an alternative, and the session keeps working. |
| 6 | Within 5 minutes, cancel the non-refundable stay | Reuses the MFA token (*Existing token*). After 5 minutes it asks again. |

## Layout

```
staywise/            agent backend
  auth/broker.py     which token each request carries; how to get a stronger one
  auth/stepup.py     pauses a call for the MFA popup (approve / decline / timeout)
  auth/thunder.py    OAuth client: auth code + PKCE, refresh, client credentials, token exchange
  auth/policy.py     loads config/stepup_policy.yaml
  auth/challenge.py  WWW-Authenticate parser (RFC 6750, RFC 9470)
  transport.py       httpx transport under the MCP SDK: attach token, step up, retry
  session.py         one guest: tokens, per-guest MCP connections, agent, event bus
  http_api.py        BFF routes, SSE, step-up popup endpoints, mock phone
servers/             the four MCP servers, the auth guard (guard.py) and the SQLite store
web/                 React UI (chat, security timeline, step-up dialog, mock phone)
wso2/                gateway proxies; Agent Manager env template; thunder/ setup + teardown scripts and VERIFIED.md
Dockerfile           image for the hosted agent (backend + built web UI)
config/              MCP server lists, step-up policy
```

## Checks

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/pytest -q                                   # servers, guard, broker, transport, BFF, end-to-end step-up
(cd web && npm run lint && npm run typecheck && npm test)
```

`tests/test_stepup_flow.py` runs the demo script in memory. It uses the real guarded servers,
transport, broker and coordinator, a fake Thunder with 1.0.0's exchange semantics, and a
scripted model.

## Clean up

```bash
KUBECONFIG=<(k3d kubeconfig get amp-local) kubectl -n default-default delete mcp -l app.kubernetes.io/part-of=staywise
wso2/thunder/thunder-teardown.sh --from-cluster --remove-env-file
rm -rf data/
```
