"""Small pure pieces: challenge parsing, policy, mock phone text extraction, env files, settings."""

from __future__ import annotations

from pathlib import Path

import httpx
import jwt
import pytest

from servers.runner import UNCONFIGURED_CLIENT, GuardSettings
from staywise.auth.challenge import parse_challenge
from staywise.auth.policy import PolicyError, Strategy, load_policy
from staywise.auth.thunder import ThunderClient
from staywise.envfile import load_env_files, parse_env_file
from staywise.events import EventBus
from staywise.mock_phone import PhoneInbox, extract_text
from staywise.settings import Settings, SettingsError
from tests import fakes
from tests.conftest import make_token


def test_parses_gateway_and_server_challenges() -> None:
    gateway = parse_challenge(
        403,
        'Bearer resource_metadata="http://gw/x/.well-known/oauth-protected-resource", error="insufficient_scope", '
        'scope="booking:write payment:charge", error_description="Forbidden: insufficient permissions"',
    )
    assert gateway.error == "insufficient_scope" and gateway.needs_more
    assert gateway.scopes == {"booking:write", "payment:charge"}
    assert gateway.resource_metadata == "http://gw/x/.well-known/oauth-protected-resource"

    expired = parse_challenge(401, 'Bearer error="invalid_token", error_description="expired"')
    assert expired.needs_new_token and not expired.needs_more

    rfc9470 = parse_challenge(401, 'Bearer error="insufficient_user_authentication", acr_values="mfa", max_age=300')
    assert rfc9470.needs_more and rfc9470.acr_values == "mfa" and rfc9470.max_age == 300


@pytest.mark.parametrize("header", [None, "", "Basic realm=x", "Bearer"])
def test_missing_or_foreign_challenge_is_bare(header: str | None) -> None:
    challenge = parse_challenge(403, header)
    assert challenge.error is None and not challenge.scopes


def test_quoted_values_with_escapes_and_commas() -> None:
    challenge = parse_challenge(403, r'Bearer error="insufficient_scope", error_description="a \"b\", c"')
    assert challenge.description == 'a "b", c'


def test_shipped_policy() -> None:
    policy = fakes.policy()
    assert policy.strategy_for(frozenset({"booking:write"})) == Strategy.AUTO_OBO
    assert policy.strategy_for(frozenset({"booking:write", "payment:highvalue"})) == Strategy.USER
    assert policy.strategy_for(frozenset({"ops:notify"})) == Strategy.AUTO_AGENT
    assert policy.strategy_for(frozenset({"ops:notify", "payment:highvalue"})) is None
    assert policy.strategy_for(frozenset({"admin:everything"})) is None
    assert policy.strategy_for(frozenset()) is None
    assert "payment:highvalue" not in policy.login_scopes
    assert policy.server("concierge").identity == Strategy.AUTO_AGENT


def test_policy_rejects_baseline_outside_scopes(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text(
        "scopes: {a: auto-obo}\nservers: {s: {baseline: [b]}}\nuser_stepup: {acr_values: mfa}\n", encoding="utf-8"
    )
    with pytest.raises(PolicyError, match="baseline"):
        load_policy(path)


def test_policy_rejects_unknown_strategy(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("scopes: {a: maybe}\nservers: {}\nuser_stepup: {}\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (b'{"to":"+9477","body":"Code 123456 for StayWise"}', "application/json", "Code 123456 for StayWise"),
        (b'{"data":{"message":"Your OTP is 7788"},"meta":{"id":"abc"}}', "application/json", "Your OTP is 7788"),
        (b"to=%2B9477&message=OTP+4321", "application/x-www-form-urlencoded", "OTP 4321"),
        (b"plain 99887766 text", "text/plain", "plain 99887766 text"),
    ],
)
def test_extracts_sms_text(body: bytes, content_type: str, expected: str) -> None:
    assert extract_text(body, content_type) == expected


def test_inbox_finds_otp_and_notifies() -> None:
    seen = []
    inbox = PhoneInbox(seen.append)
    message = inbox.receive(b'{"body": "Use 246810 to verify. Ref 12"}', "application/json")
    assert message.otp == "246810" and seen == [message] and inbox.messages() == [message]


def test_env_file_parsing_and_precedence(tmp_path: Path) -> None:
    assert parse_env_file("# c\nexport A='1'\nB = \"two\"\nnoise\nC=x=y\n") == {"A": "1", "B": "two", "C": "x=y"}
    path = tmp_path / ".env"
    path.write_text("A=from-file\nB=from-file\n", encoding="utf-8")
    environ = {"A": "already-set"}
    load_env_files([path, tmp_path / "missing"], environ)
    assert environ == {"A": "already-set", "B": "from-file"}


def test_settings_name_missing_credentials() -> None:
    with pytest.raises(SettingsError, match=r"WEB_CLIENT_ID.*thunder-setup\.sh"):
        Settings.from_env({"STEPUP_CLIENT_ID": "x"})


def test_settings_defaults(tmp_path: Path) -> None:
    settings = fakes.settings(tmp_path)
    assert settings.login_redirect_uri == "http://localhost:9100/auth/callback"
    assert settings.stepup_redirect_uri == "http://localhost:9100/auth/stepup/callback"
    assert settings.use_actor_token and settings.resource == "urn:staywise:hotel-api"


def test_thunder_plain_text_sms_with_json_content_type() -> None:
    # Observed live: Thunder's custom sender posts the rendered template as text, labelled application/json.
    body = b"Your verification code is: 123456. It expires in 5 minutes."
    message = PhoneInbox().receive(body, "application/json")
    assert message.otp == "123456" and message.text == body.decode()


def test_high_value_rules_fail_closed_without_stepup_client() -> None:
    settings = GuardSettings.from_env({})
    assert settings.stepup_client_ids == {UNCONFIGURED_CLIENT}
    assert GuardSettings.from_env({"STEPUP_CLIENT_ID": "staywise-stepup"}).stepup_client_ids == {"staywise-stepup"}


async def test_token_scopes_come_from_the_token_not_the_response_body() -> None:
    # Thunder drops unobtainable scopes silently: 200, and the token has no `scope` claim.
    access = make_token("")
    unscoped = jwt.encode(
        {k: v for k, v in jwt.decode(access, options={"verify_signature": False}).items() if k != "scope"}, "k" * 32
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"resource=urn%3Astaywise%3Ahotel-api" in request.content
        return httpx.Response(200, json={"access_token": unscoped, "scope": "payment:highvalue", "expires_in": 3600})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    thunder = ThunderClient("http://idp.test", "urn:staywise:hotel-api", http=http)
    tokens = await thunder.token_exchange(fakes.WEB, access, frozenset({"payment:highvalue"}))
    assert tokens.scopes == frozenset()


def test_base_path_comes_from_public_url(tmp_path: Path) -> None:
    assert fakes.settings(tmp_path).base_path == ""
    hosted = fakes.settings(tmp_path, PUBLIC_URL="http://default-default.agentmanager.localhost:19080/staywise/")
    assert hosted.base_path == "/staywise"
    assert hosted.login_redirect_uri == "http://default-default.agentmanager.localhost:19080/staywise/auth/callback"
    assert hosted.thunder_internal_url is None
    internal = fakes.settings(tmp_path, THUNDER_INTERNAL_URL="http://thunder.svc:8090/")
    assert internal.thunder_internal_url == "http://thunder.svc:8090"


async def test_thunder_back_channel_uses_the_internal_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"access_token": make_token("ops:notify"), "expires_in": 60})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    thunder = ThunderClient("http://idp.public:8080", "urn:r", internal_url="http://idp.svc:8090", http=http)
    await thunder.client_credentials(fakes.AGENT, frozenset({"ops:notify"}))
    assert seen == ["http://idp.svc:8090/oauth2/token"]
    assert thunder.jwks_uri == "http://idp.svc:8090/oauth2/jwks"
    assert thunder.issuer == "http://idp.public:8080"
    assert thunder.authorize_endpoint == "http://idp.public:8080/oauth2/authorize"


async def test_event_bus_replays_after_last_event_id() -> None:
    bus = EventBus()
    for n in range(3):
        bus.publish("turn_started", {"n": n})
    fresh, resumed, ahead = bus.subscribe(), bus.subscribe(after=1), bus.subscribe(after=99)
    bus.publish("turn_finished", {})
    assert [fresh.get_nowait().id for _ in range(fresh.qsize())] == [4]
    assert [resumed.get_nowait().id for _ in range(resumed.qsize())] == [2, 3, 4]
    assert [ahead.get_nowait().id for _ in range(ahead.qsize())] == [4]
