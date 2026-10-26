# ThunderID setup for StayWise: what was verified live

These results come from a throwaway container: `ghcr.io/thunder-id/thunderid:1.0.0`, SQLite, `http_only: true`, `public_url: http://localhost:18090`. The container is not the AMP cluster. The scripts ran against it end to end: setup, setup again, teardown, teardown again, then setup on a clean server. Every claim below was observed on 2026-10-24 unless it is marked as unverified. Signatures, secrets and OTP values are left out.

## How the setup works

- `thunder-setup.sh` sends `staywise-resources.yaml` to `POST /import`, which upserts each resource by a fixed id. Secrets go in the request's `variables` and the server substitutes them, so the YAML file holds no secrets.
  - On the first run every resource reported `create`.
  - On the second run every resource reported `update`, and `.env.thunder` came out byte-identical because secrets are reused from the existing file.
- Nothing is written to stdout. A grep of the stderr log for each generated secret found zero matches.
- `thunder-teardown.sh` deletes resources by name or handle in dependency order:
  1. roles
  2. apps
  3. agent
  4. user
  5. flows
  6. SMS connection
  7. resource server, after its actions and then its resources

  A second teardown run reported `not found` for everything and exited 0.

### Admin token

- The token request is `client_credentials` with `scope=system`.
- Without a `resource` parameter the server returns `400 invalid_target` ("no default resource server is configured").
- The script then retries with `resource=https://localhost:8090/mcp`. That is the identifier of the bootstrap "System" resource server and is hardcoded in Thunder's bootstrap. The retry succeeds.
- `THUNDER_ADMIN_RESOURCE` overrides this if AMP uses a different identifier.

## Observed token shapes

The `scope` claim is always a single space-separated string. Every access token has `typ: at+jwt`, is signed RS256 and has `iss` equal to the issuer (`http://localhost:18090` locally).

### 1. Agent client_credentials

Request: `resource=urn:staywise:hotel-api&scope=ops:notify`.

```json
{"aud":"urn:staywise:hotel-api","client_id":"staywise-concierge-agent","grant_type":"client_credentials",
 "name":"staywise-concierge-agent","scope":"ops:notify","sub":"<agent entity id>"}
```

- `sub` is the agent's entity id. It is not the client id.
- Without a `scope` parameter the token has no `scope` claim at all.
- Asking for an extra scope, for example `hotel:read`, drops it silently.

### 2. staywise-web authorization_code + PKCE (S256)

- **Access token** (`expires_in` 3600):
  ```json
  {"aud":"urn:staywise:hotel-api","client_id":"staywise-web","grant_type":"authorization_code","name":"Demo Guest",
   "scope":"openid profile hotel:read booking:read booking:write payment:charge","sub":"<user id (UUID)>","username":"guest"}
  ```
  Extra claims: `aci`, `tfid` (token family id), `jti`, `iat`, `nbf`, `exp`.
- **ID token**:
  ```json
  {"aud":"staywise-web","auth_time":...,"nonce":"...","sub":"<user id>","name":"Demo Guest","given_name":"Demo","family_name":"Guest","username":"guest"}
  ```
  There is no `acr` claim.
- **Without PKCE**, the server redirects with `error=invalid_request&error_description=code_challenge is required for this application`.
- **Without `resource`**, the server redirects with `error=invalid_target`, because no default resource server is configured. Always send `resource=urn:staywise:hotel-api`.

### 3. Token exchange (RFC 8693)

Every request in this section used `subject_token=<user AT>`, `actor_token=<agent AT>`, `actor_token_type=urn:ietf:params:oauth:token-type:access_token` and `resource=urn:staywise:hotel-api`.

- **staywise-web exchanges, `scope=booking:write`**: works.
  ```json
  {"act":{"iss":"<issuer>","sub":"<agent entity id>"},"aud":"urn:staywise:hotel-api","client_id":"staywise-web",
   "grant_type":"urn:ietf:params:oauth:grant-type:token-exchange","scope":"booking:write","sub":"<user id>","name":"Demo Guest","username":"guest"}
  ```
  The response has `issued_token_type=...:access_token` and `expires_in` 3600, taken from the exchanging client's access-token TTL.
- **`scope=payment:highvalue`**: the scope is dropped silently. The response is still HTTP 200 with an access token, but it has **no `scope` claim**. It is not an `invalid_scope` error.
- **`scope=booking:write payment:highvalue`**: the token has `scope:"booking:write"`.
- **staywise-concierge-agent as the exchanging client**, with the web-issued subject token: works.
  - With `actor_token`, the token has `client_id:"staywise-concierge-agent"` and the same `act` claim. It has no `name`/`username` claims.
  - Without `actor_token`, the token has no `act` claim.

### 4. Step-up (staywise-stepup)

Request: `acr_values=mfa&prompt=login&scope=openid payment:highvalue&resource=urn:staywise:hotel-api`.

- **Flow steps** observed through `/flow/execute`:
  1. Username and password. No action chooser appears because the single option was auto-selected for `mfa`.
  2. OTP, with `additionalData {"otpLength":"6","otpNumericOnly":"true","smsSent":"true"}`.
  3. `COMPLETE`.
- **Wrong OTP**: the server re-prompts for the OTP. No new SMS is sent.
- **Access token** (`expires_in` 300, no refresh token):
  ```json
  {"aud":"urn:staywise:hotel-api","client_id":"staywise-stepup","grant_type":"authorization_code","scope":"openid payment:highvalue","sub":"<user id>","username":"guest"}
  ```
- **ID token**: `{"acr":"mfa","aud":"staywise-stepup","auth_time":...,"nonce":"...","sub":"<user id>"}`.
- **Without `acr_values`**, the app's configured `acrValues=[mfa]` applies and `acr` is still `"mfa"`.
- **Without `resource`**, the server redirects with `error=invalid_target`.
- **staywise-stepup cannot do token exchange**: the server returns `unauthorized_client`.

### 5. Refresh token on staywise-web

- A refresh works: it returns a new access token and an ID token, and the **refresh token rotates**.
- A refresh with `scope=hotel:read` downscopes correctly. The refreshed access token keeps `grant_type:"authorization_code"`.
- **Replaying an already-used refresh token** returns `invalid_grant`. It **revokes the whole token family**: the newer refresh token and the user access token both stop working, and introspection returns `{"active":false}`. The client must always store the newest refresh token.

## Custom SMS sender: the exact request ThunderID sends

```
POST <MOCK_PHONE_URL>
Content-Type: application/json
X-Mock-Phone-Key: <MOCK_PHONE_KEY>
User-Agent: Go-http-client/1.1

Your verification code is: 123456. It expires in 5 minutes.
```

- The body is **plain text, not JSON**, even though the `Content-Type` header says JSON. This comes from the built-in `sms_otp` template.
- The **recipient number is not sent**. The custom client posts only the rendered body.
- The OTP is 6 numeric digits and is valid for 300 s, both set in the flow.
- A non-2xx response from the gateway fails the send. This was read in `custom_client.go`; it was **not tested live**.

## Deviations and limits found

1. **The app's `scopes` list does NOT restrict permission scopes in Thunder 1.0.0.** Permissions come from the user's roles only.
   - When staywise-web asked for `payment:highvalue` at `/oauth2/authorize`, it **received it**.
   - staywise-web can also exchange a step-up token into a 3600 s token with `payment:highvalue` and `client_id:"staywise-web"`. That was observed live.
   - Mitigation on the API side:
     - require `payment:highvalue` **and** `client_id == STEPUP_CLIENT_ID`, plus a fresh `iat`
     - never request `payment:highvalue` from staywise-web
   - Making Thunder itself refuse would need a separate resource server for high-value payments, which breaks the "one resource" contract.
2. **`acr_values=mfa` is honored only if the server registers the auth class.** The deployment.yaml needs:
   ```yaml
   oauth:
     auth_class:
       amrs: ["pwd", "otp"]
       acr_amr:
         mfa: ["pwd", "otp"]
   ```
   - Without it, Thunder rejects `acrValues` on the app.
   - The setup script detects this through discovery `acr_values_supported` and creates the app without `acrValues`, writing `STEPUP_ACR_ENFORCED=false`.
   - In that mode (tested live by restarting the container without the block):
     - `acr_values=mfa` is ignored, not rejected.
     - The flow still requires password and OTP.
     - The ID token still has `acr:"mfa"`.
     - A headless client first sees a one-option chooser step (`actions:["action_login"]`, no inputs). A browser gets the same form.
   - Thunder's upstream Helm chart has no value for `auth_class`.
3. **Agents**: Thunder's first-class `agent` entity supports `client_credentials` and token exchange, so the agent fallback was not needed. The agent has no `owner` set.
4. **Headless login sequence** used for these tests:
   1. `GET /oauth2/authorize` returns 302 to `<issuer>/gate/signin?applicationId&authId&executionId`.
   2. `POST /flow/execute {"executionId"}` returns the first step and a `challengeToken`.
   3. `POST /flow/execute {"executionId","challengeToken","action"?,"inputs":{...}}` repeats until the response has `flowStatus: COMPLETE` and `assertion`.
   4. `POST /oauth2/auth/callback {"authId","assertion"}` returns `{"redirect_uri":"...?code=...&state=...&iss=..."}`.
   5. `POST /oauth2/token`.

## Not verified here

- **The AMP cluster itself.** Nothing was run against it and no Kubernetes secret was read.
  - Whether AMP's Thunder accepts `POST /import` or uses a read-only declarative store is unknown.
  - The admin-token `resource` that AMP's system client needs is unknown.
  - Whether `auth_class` is configured in AMP is unknown.
- **Browser rendering of the gate UI** for the two custom flows. They were driven only through the API.
- **The real mock-phone endpoint** at `192.168.5.2:9100`. Locally the container reached a host listener at both `host.docker.internal:19100` and `192.168.5.2:19100`.
