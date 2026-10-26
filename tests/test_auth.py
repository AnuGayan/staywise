import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from concierge.auth import (
    AuthConfigError,
    AuthError,
    OidcClient,
    OidcSettings,
    TokenGrant,
    WebAuth,
    load_members,
)

ISSUER = "http://idp.test"
SETTINGS = OidcSettings(
    issuer=ISSUER,
    client_id="hardrock-concierge-web",
    client_secret="s3cret",
    public_url="http://localhost:9100",
    resource="urn:hardrock:concierge-api",
)
MEMBERS = {"alex": "HR-100245"}


class FakeThunder:
    """Discovery, JWKS and token endpoints backed by one RSA key; records token requests."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.token_requests: list[httpx.Request] = []
        self.next_id_token = ""
        self.token_status = 200

    def id_token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": SETTINGS.client_id,
            "sub": "user-1",
            "iat": now,
            "exp": now + 3600,
            "nonce": "n",
            "username": "alex",
            "name": "Alex Morgan",
        }
        claims.update(overrides)
        return jwt.encode({k: v for k, v in claims.items() if v is not None}, self.key, "RS256", {"kid": self.kid})

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth2/authorize",
                    "token_endpoint": f"{ISSUER}/oauth2/token",
                    "jwks_uri": f"{ISSUER}/oauth2/jwks",
                },
            )
        if request.url.path == "/oauth2/jwks":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": self.kid, "alg": "RS256", "use": "sig"}]})
        if request.url.path == "/oauth2/token":
            self.token_requests.append(request)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self.next_id_token, "expires_in": 3600})
        return httpx.Response(404)


@pytest.fixture
def thunder() -> FakeThunder:
    return FakeThunder()


def client_for(thunder: FakeThunder) -> OidcClient:
    return OidcClient(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(thunder.handler)))


def test_settings_from_env_requires_the_client_secret() -> None:
    with pytest.raises(AuthConfigError, match="CONCIERGE_CLIENT_SECRET"):
        OidcSettings.from_env({})


def test_settings_from_env_defaults_and_overrides() -> None:
    settings = OidcSettings.from_env({"CONCIERGE_CLIENT_SECRET": "x", "CONCIERGE_PUBLIC_URL": "https://c.example/"})
    assert settings.issuer == "http://default-idp.amp.localhost:8080"
    assert settings.client_id == "hardrock-concierge-web"
    assert settings.redirect_uri == "https://c.example/auth/callback"
    assert settings.secure_cookies is True
    assert SETTINGS.secure_cookies is False


def test_load_members_lowercases_usernames(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text('{"Alex": "HR-100245"}', encoding="utf-8")
    assert load_members(path) == {"alex": "HR-100245"}


@pytest.mark.parametrize("content", ["[1]", '{"alex": 5}', "{nope"])
def test_load_members_rejects_bad_files(tmp_path: Path, content: str) -> None:
    path = tmp_path / "m.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AuthConfigError):
        load_members(path)


def test_shipped_member_map_covers_the_demo_guests() -> None:
    members = load_members(Path(__file__).resolve().parent.parent / "config" / "members.json")
    assert members == {"alex": "HR-100245", "priya": "HR-100377", "daniel": "HR-100512"}


async def test_authorization_url_carries_pkce_resource_and_prompt(thunder: FakeThunder) -> None:
    url = await client_for(thunder).authorization_url("st", "no", "challenge")
    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{ISSUER}/oauth2/authorize"
    assert query == {
        "response_type": "code",
        "client_id": "hardrock-concierge-web",
        "redirect_uri": "http://localhost:9100/auth/callback",
        "scope": "openid profile hotel:read bookings:read bookings:write unity:read",
        "resource": "urn:hardrock:concierge-api",
        "state": "st",
        "nonce": "no",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "prompt": "login",
    }


async def test_exchange_code_uses_basic_auth_and_verifier(thunder: FakeThunder) -> None:
    thunder.next_id_token = "tok"
    grant = await client_for(thunder).exchange_code("the-code", "the-verifier")
    assert grant == TokenGrant(id_token="tok", expires_in=3600)
    request = thunder.token_requests[0]
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"hardrock-concierge-web:s3cret").decode()
    form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
    assert form["code"] == "the-code"
    assert form["code_verifier"] == "the-verifier"
    assert form["grant_type"] == "authorization_code"


async def test_exchange_code_reports_rejection(thunder: FakeThunder) -> None:
    thunder.token_status = 400
    with pytest.raises(AuthError, match="invalid_grant"):
        await client_for(thunder).exchange_code("c", "v")


async def test_verify_id_token_accepts_a_valid_token(thunder: FakeThunder) -> None:
    claims = await client_for(thunder).verify_id_token(thunder.id_token(), "n")
    assert (claims["sub"], claims["username"]) == ("user-1", "alex")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"iss": "http://evil.test"}, "not valid"),
        ({"aud": "someone-else"}, "not valid"),
        ({"exp": int(time.time()) - 3600}, "not valid"),
        ({"nonce": "other"}, "nonce mismatch"),
        ({"iat": None}, "not valid"),
    ],
)
async def test_verify_id_token_rejects_bad_claims(
    thunder: FakeThunder, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(AuthError, match=message):
        await client_for(thunder).verify_id_token(thunder.id_token(**overrides), "n")


async def test_verify_id_token_rejects_a_foreign_signature(thunder: FakeThunder) -> None:
    other = FakeThunder()
    with pytest.raises(AuthError, match="not valid"):
        await client_for(thunder).verify_id_token(other.id_token(), "n")


async def test_verify_id_token_refetches_keys_after_rotation(thunder: FakeThunder) -> None:
    client = client_for(thunder)
    await client.verify_id_token(thunder.id_token(), "n")
    thunder.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    thunder.kid = "k2"
    claims = await client.verify_id_token(thunder.id_token(), "n")
    assert claims["sub"] == "user-1"


class FakeIdp:
    """Hands back whatever claims the test sets, after checking the PKCE pair matches."""

    def __init__(self, claims: dict[str, Any]) -> None:
        self.claims = claims
        self.challenge = ""
        self.state = ""
        self.nonce = ""

    async def authorization_url(self, state: str, nonce: str, code_challenge: str) -> str:
        self.state, self.nonce, self.challenge = state, nonce, code_challenge
        return f"{ISSUER}/oauth2/authorize?state={state}"

    async def exchange_code(self, code: str, code_verifier: str) -> TokenGrant:
        digest = hashlib.sha256(code_verifier.encode()).digest()
        assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == self.challenge
        assert code == "good-code"
        return TokenGrant(id_token="id", expires_in=900)

    async def verify_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        assert nonce == self.nonce
        return self.claims


async def test_web_auth_round_trip_binds_the_member_id() -> None:
    idp = FakeIdp({"sub": "user-1", "username": "Alex", "name": "Alex Morgan"})
    auth = WebAuth(idp, MEMBERS)
    await auth.begin_login()
    session_id, ttl = await auth.complete_login(idp.state, "good-code")
    guest = auth.guest(session_id)
    assert ttl == 900
    assert guest is not None
    assert (guest.username, guest.name, guest.member_id) == ("Alex", "Alex Morgan", "HR-100245")
    auth.logout(session_id)
    assert auth.guest(session_id) is None


async def test_web_auth_state_is_single_use() -> None:
    idp = FakeIdp({"sub": "u", "username": "alex"})
    auth = WebAuth(idp, MEMBERS)
    await auth.begin_login()
    await auth.complete_login(idp.state, "good-code")
    with pytest.raises(AuthError, match="expired or was already used"):
        await auth.complete_login(idp.state, "good-code")


async def test_web_auth_rejects_stale_logins_and_expires_sessions() -> None:
    now = [1000.0]
    idp = FakeIdp({"sub": "u", "username": "alex"})
    auth = WebAuth(idp, MEMBERS, clock=lambda: now[0])
    await auth.begin_login()
    now[0] += 601
    with pytest.raises(AuthError, match="expired"):
        await auth.complete_login(idp.state, "good-code")

    await auth.begin_login()
    session_id, _ = await auth.complete_login(idp.state, "good-code")
    now[0] += 899
    assert auth.guest(session_id) is not None
    now[0] += 1
    assert auth.guest(session_id) is None


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"sub": "u", "username": "mallory"}, "not linked to a Unity"),
        ({"sub": "u"}, "no username claim"),
    ],
)
async def test_web_auth_requires_a_linked_username(claims: dict[str, Any], message: str) -> None:
    idp = FakeIdp(claims)
    auth = WebAuth(idp, MEMBERS)
    await auth.begin_login()
    with pytest.raises(AuthError, match=message):
        await auth.complete_login(idp.state, "good-code")


def test_unknown_or_missing_session_is_anonymous() -> None:
    auth = WebAuth(FakeIdp({}), MEMBERS)
    assert auth.guest(None) is None
    assert auth.guest("nope") is None
    auth.logout("nope")


def test_backchannel_rewrites_issuer_urls_onto_the_internal_url() -> None:
    settings = OidcSettings.from_env(
        {
            "CONCIERGE_CLIENT_SECRET": "x",
            "THUNDER_ISSUER": "http://default-idp.amp.localhost:8080",
            "THUNDER_INTERNAL_URL": "http://thunder.svc:8090/",
            "CONCIERGE_PUBLIC_URL": "http://gw.localhost:19080/melody/",
        }
    )
    assert (
        settings.backchannel("http://default-idp.amp.localhost:8080/oauth2/token")
        == "http://thunder.svc:8090/oauth2/token"
    )
    assert settings.backchannel("http://elsewhere/oauth2/jwks") == "http://elsewhere/oauth2/jwks"
    assert settings.base_path == "/melody"
    assert settings.redirect_uri == "http://gw.localhost:19080/melody/auth/callback"
    assert SETTINGS.base_path == ""
    assert SETTINGS.backchannel(f"{ISSUER}/oauth2/token") == f"{ISSUER}/oauth2/token"


async def test_internal_url_carries_every_backchannel_call(thunder: FakeThunder) -> None:
    internal = OidcSettings(**{**SETTINGS.__dict__, "internal_url": "http://thunder.internal:8090"})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        # The internal service answers as the public issuer, like a real ThunderID does.
        return thunder.handler(request)

    client = OidcClient(internal, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    authorize = await client.authorization_url("s", "n", "c")
    thunder.next_id_token = thunder.id_token()
    grant = await client.exchange_code("code", "verifier")
    await client.verify_id_token(grant.id_token, "n")
    assert authorize.startswith(f"{ISSUER}/oauth2/authorize?")  # the browser still goes to the public issuer
    assert seen == [
        "thunder.internal/.well-known/openid-configuration",
        "thunder.internal/oauth2/token",
        "thunder.internal/oauth2/jwks",
    ]
