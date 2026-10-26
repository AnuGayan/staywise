import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from concierge.auth import OidcSettings
from concierge.e2e import (
    Context,
    LoginError,
    Outcome,
    Turn,
    check_gateway_rejects,
    headless_login,
    load_env_file,
    parse_args,
    report,
    run,
    run_scenario,
    scenario,
)

ISSUER = "http://idp.test"
SETTINGS = OidcSettings(
    ISSUER, "hardrock-concierge-web", "s3cret", "http://localhost:9100", "urn:hardrock:concierge-api"
)
MEMBER = "HR-100245"


class FakeThunder:
    """authorize -> gate redirect; /flow/execute asks for username+password then completes; callback; token."""

    def __init__(self) -> None:
        self.state = ""
        self.flow_requests: list[dict[str, Any]] = []
        self.token_form: dict[str, str] = {}
        self.flow_error = False
        self.extra_input = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth2/authorize":
            query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
            self.state = query["state"]
            assert query["code_challenge_method"] == "S256" and query["resource"] == SETTINGS.resource
            return httpx.Response(
                302, headers={"location": f"{ISSUER}/gate/signin?applicationId=a&authId=AUTH&executionId=EX"}
            )
        if path == "/flow/execute":
            body = json.loads(request.content)
            self.flow_requests.append(body)
            if self.flow_error:
                return httpx.Response(200, json={"flowStatus": "ERROR", "error": {"code": "FLW-1", "message": "bad"}})
            if "inputs" not in body:
                inputs = [{"identifier": "username"}, {"identifier": "password"}]
                if self.extra_input:
                    inputs.append({"identifier": "otp"})
                return httpx.Response(
                    200,
                    json={
                        "executionId": "EX",
                        "flowStatus": "INCOMPLETE",
                        "challengeToken": "CT",
                        "data": {"inputs": inputs, "actions": [{"ref": "action_login"}]},
                    },
                )
            return httpx.Response(200, json={"executionId": "EX", "flowStatus": "COMPLETE", "assertion": "ASSERT"})
        if path == "/oauth2/auth/callback":
            assert json.loads(request.content) == {"authId": "AUTH", "assertion": "ASSERT"}
            return httpx.Response(
                200, json={"redirect_uri": f"http://localhost:9100/auth/callback?code=CODE&state={self.state}"}
            )
        if path == "/oauth2/token":
            self.token_form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            return httpx.Response(200, json={"access_token": "user-at", "expires_in": 3600})
        return httpx.Response(404)


def idp(thunder: FakeThunder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(thunder.handler))


def test_load_env_file_tolerates_spaces_quotes_and_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# c\nA='1'\nB = '2'\nC=\"3\"\nnot a pair\n", encoding="utf-8")
    assert load_env_file(path) == {"A": "1", "B": "2", "C": "3"}
    assert load_env_file(tmp_path / "missing") == {}


async def test_headless_login_walks_the_flow_and_redeems_the_code() -> None:
    thunder = FakeThunder()
    assert await headless_login(idp(thunder), SETTINGS, "alex", "pw") == "user-at"
    first, second = thunder.flow_requests
    assert first == {"executionId": "EX"}
    assert second == {
        "executionId": "EX",
        "challengeToken": "CT",
        "inputs": {"username": "alex", "password": "pw"},
        "action": "action_login",
    }
    assert thunder.token_form["code"] == "CODE"
    assert thunder.token_form["redirect_uri"] == "http://localhost:9100/auth/callback"
    assert thunder.token_form["code_verifier"]


@pytest.mark.parametrize(("setup", "message"), [("flow_error", "sign-in flow failed"), ("extra_input", "otp")])
async def test_headless_login_reports_flow_problems(setup: str, message: str) -> None:
    thunder = FakeThunder()
    setattr(thunder, setup, True)
    with pytest.raises(LoginError, match=message):
        await headless_login(idp(thunder), SETTINGS, "alex", "pw")


async def test_headless_login_reports_an_authorize_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": f"{ISSUER}/gate/error?error=invalid_request&errorMessage=Invalid+client_id"}
        )

    with pytest.raises(LoginError, match="Invalid client_id"):
        await headless_login(httpx.AsyncClient(transport=httpx.MockTransport(handler)), SETTINGS, "alex", "pw")


def call(name: str, result: dict[str, Any] | str, **arguments: Any) -> dict[str, Any]:
    return {
        "name": name,
        "arguments": {"member_id": MEMBER, **arguments},
        "result": result if isinstance(result, str) else json.dumps(result),
    }


class ScriptedAgent:
    """Answers each scenario prompt with the right tool call, unless told to skip or misbehave."""

    def __init__(self, skip_first: set[str] | None = None, foreign_member: bool = False) -> None:
        self.skip_first = set(skip_first or ())
        self.foreign_member = foreign_member
        self.prompts: list[str] = []
        self.booked = ""

    async def __call__(self, message: str, conversation_id: str, token: str | None) -> Turn:
        self.prompts.append(message)
        if token is None or token == "aaa.bbb.ccc":
            return Turn(401, "Unauthorized", [], None, 0.0)
        tool = next(
            step.tool for step in scenario() if step.prompt.split()[0] in message and _matches(step.prompt, message)
        )
        if tool in self.skip_first:
            self.skip_first.discard(tool)
            return Turn(200, "I can't check that right now.", [], MEMBER, 0.1)
        results = {
            "get_hotel_info": {"check_in_time": "16:00"},
            "list_room_types": {"room_types": [{"code": "DLXK"}]},
            "check_availability": {"check_in": "2026-12-10", "available": [{"room_code": "DLXK"}]},
            "create_booking": {"confirmation_number": "HRM-ABC234", "check_in": "2026-12-10", "status": "upcoming"},
            "get_booking": {"confirmation_number": "HRM-ABC234"},
            "list_bookings": {"bookings": [{"confirmation_number": "HRM-ABC234"}]},
            "cancel_booking": {"confirmation_number": "HRM-ABC234", "status": "cancelled"},
            "get_unity_balance": {"points_balance": 18450, "tier": "Legend"},
            "get_points_activity": {"activity": [{"points": 2500}]},
        }
        tool_call = call(tool, results[tool])
        if self.foreign_member:
            tool_call["arguments"]["member_id"] = "HR-100377"
        return Turn(200, "done", [tool_call], MEMBER, 0.1)


def _matches(template: str, message: str) -> bool:
    return template.split("{")[0].strip() in message


def context() -> Context:
    return Context(member_id=MEMBER, check_in="2026-12-10", check_out="2026-12-12")


async def test_full_scenario_passes_and_cleans_up() -> None:
    ctx = context()
    outcomes = await run_scenario(ScriptedAgent(), "user-at", ctx)
    assert [o.status for o in outcomes] == ["PASS"] * len(scenario())
    assert ctx.confirmation is None  # cancelled again
    assert "HRM-ABC234" in outcomes[3].detail


async def test_a_skipped_tool_is_retried_with_a_hint() -> None:
    agent = ScriptedAgent(skip_first={"get_unity_balance"})
    outcomes = await run_scenario(agent, "user-at", context())
    balance = next(o for o in outcomes if o.name == "points balance")
    assert (balance.status, balance.attempts) == ("PASS", 2)
    assert any(p.endswith("Use the get_unity_balance tool.") for p in agent.prompts)


async def test_failed_booking_skips_the_steps_that_need_it() -> None:
    agent = ScriptedAgent(skip_first={"create_booking"})
    outcomes = await run_scenario(agent, "user-at", context(), retries=0)
    by_name = {o.name: o.status for o in outcomes}
    assert by_name["book a room"] == "FAIL"
    assert (by_name["get booking"], by_name["list bookings"], by_name["cancel booking"]) == ("SKIP", "SKIP", "SKIP")


async def test_a_call_for_another_member_fails() -> None:
    outcomes = await run_scenario(ScriptedAgent(foreign_member=True), "user-at", context(), retries=0)
    assert outcomes[0].status == "FAIL"
    assert "for member HR-100377" in outcomes[0].detail


async def test_tool_errors_fail_the_step() -> None:
    async def refusing(message: str, conversation_id: str, token: str | None) -> Turn:
        refused = call("get_hotel_info", "Error: not authorized to use get_hotel_info: refused")
        return Turn(200, "sorry", [refused], MEMBER, 0.1)

    outcomes = await run_scenario(refusing, "user-at", context(), retries=0)
    assert outcomes[0].status == "FAIL"
    assert "not authorized" in outcomes[0].detail


async def test_gateway_must_reject_missing_and_forged_tokens() -> None:
    outcomes = await check_gateway_rejects(ScriptedAgent())
    assert [o.status for o in outcomes] == ["PASS", "PASS"]

    async def lets_everyone_in(message: str, conversation_id: str, token: str | None) -> Turn:
        return Turn(200, "hi", [], MEMBER, 0.0)

    assert [o.status for o in await check_gateway_rejects(lets_everyone_in)] == ["FAIL", "FAIL"]


def test_report_counts_results() -> None:
    text = report([Outcome("a", "PASS", "ok", 1.0), Outcome("b", "FAIL", "bad", 2.0, 2), Outcome("c", "SKIP", "-")])
    assert "FAIL b" in text and "(attempt 2)" in text
    assert text.endswith("1 passed, 1 failed, 1 skipped")


async def test_run_needs_the_demo_password(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = parse_args(["--env-file", str(tmp_path / "none")])
    assert await run(args, {}) == 2
    assert "DEMO_PASSWORD is not set" in capsys.readouterr().err


async def test_run_rejects_an_unlinked_user(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = parse_args(["--user", "mallory", "--env-file", str(tmp_path / "none")])
    assert await run(args, {"DEMO_PASSWORD": "pw", "CONCIERGE_CLIENT_SECRET": "s"}) == 2
    assert "mallory has no Unity membership" in capsys.readouterr().err
