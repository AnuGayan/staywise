from datetime import date

import pytest

from servers._common import base_parser, parse_date, require_member


def test_parse_date_accepts_iso_and_trims() -> None:
    assert parse_date(" 2026-12-12 ", "check_in") == date(2026, 12, 12)


@pytest.mark.parametrize("value", ["12/12/2026", "next friday", ""])
def test_parse_date_names_the_field_and_format(value: str) -> None:
    with pytest.raises(ValueError, match="check_out must be a date in YYYY-MM-DD"):
        parse_date(value, "check_out")


def test_require_member_normalises_case_and_space() -> None:
    assert require_member(" hr-100245 ", {"HR-100245"}) == "HR-100245"


def test_require_member_rejects_unknown_ids() -> None:
    with pytest.raises(ValueError, match="unknown Unity member id 'HR-1'"):
        require_member("HR-1", {"HR-100245"})


def test_base_parser_defaults_and_repeatable_allowed_host() -> None:
    args = base_parser(None, 8101).parse_args(["--allowed-host", "a", "--allowed-host", "b"])
    assert (args.transport, args.host, args.port, args.allowed_host) == (
        "streamable-http",
        "127.0.0.1",
        8101,
        ["a", "b"],
    )
