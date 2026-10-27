from pathlib import Path

import jwt
import pytest

from concierge.identity import Guest, IdentityError, MemberMapError, bearer_token, guest_from_token, load_members

MEMBERS = {"alex": "HR-100245"}


def user_token(**claims: object) -> str:
    # Signed with a throwaway key: the agent must accept it without checking the signature.
    return jwt.encode({"sub": "user-1", "username": "alex", **claims}, "not-the-real-key-but-32-bytes-long", "HS256")


def test_load_members_lowercases_usernames(tmp_path: Path) -> None:
    path = tmp_path / "m.json"
    path.write_text('{"Alex": "HR-100245"}', encoding="utf-8")
    assert load_members(path) == {"alex": "HR-100245"}


@pytest.mark.parametrize("content", ["[1]", '{"alex": 5}', "{nope"])
def test_load_members_rejects_bad_files(tmp_path: Path, content: str) -> None:
    path = tmp_path / "m.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(MemberMapError):
        load_members(path)


def test_shipped_member_map_covers_the_demo_guests() -> None:
    members = load_members(Path(__file__).resolve().parent.parent / "config" / "members.json")
    assert {k: members[k] for k in ("alex", "priya", "daniel")} == {
        "alex": "HR-100245",
        "priya": "HR-100377",
        "daniel": "HR-100512",
    }
    assert [members[f"loadtest{i:02d}"] for i in (1, 50)] == ["HR-300001", "HR-300050"]
    assert len(members) == 53


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"authorization": "Bearer abc"}, "abc"),
        ({"authorization": "bearer  abc "}, "abc"),
        ({"x-forwarded-authorization": "Bearer fwd"}, "fwd"),
        ({"authorization": "", "x-forwarded-authorization": "Bearer fwd"}, "fwd"),
        ({"x-forwarded-authorization": "aaa.bbb.ccc"}, "aaa.bbb.ccc"),
        ({"authorization": "aaa.bbb.ccc"}, "aaa.bbb.ccc"),
        ({"x-forwarded-authorization": "not-a-jwt"}, None),
        ({"authorization": "Basic xyz"}, None),
        ({"authorization": "Bearer "}, None),
        ({}, None),
    ],
)
def test_bearer_token_reads_authorization_or_the_forwarded_header(
    headers: dict[str, str], expected: str | None
) -> None:
    assert bearer_token(headers) == expected


def test_guest_from_token_maps_username_without_verifying() -> None:
    guest = guest_from_token(user_token(username="Alex"), MEMBERS)
    assert guest == Guest(subject="user-1", username="Alex", member_id="HR-100245")


def test_guest_from_token_accepts_preferred_username() -> None:
    token = jwt.encode({"sub": "u", "preferred_username": "alex"}, "k" * 32, "HS256")
    assert guest_from_token(token, MEMBERS).member_id == "HR-100245"


@pytest.mark.parametrize(
    ("token", "status", "message"),
    [
        ("not-a-jwt", 401, "not a JWT"),
        (jwt.encode({"sub": "u"}, "k" * 32, "HS256"), 401, "no sub/username"),
        (jwt.encode({"sub": "u", "username": "mallory"}, "k" * 32, "HS256"), 403, "not linked to a Unity"),
    ],
)
def test_guest_from_token_errors_carry_an_http_status(token: str, status: int, message: str) -> None:
    with pytest.raises(IdentityError, match=message) as caught:
        guest_from_token(token, MEMBERS)
    assert caught.value.status == status
