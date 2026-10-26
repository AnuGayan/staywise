"""StayWise payments MCP server: charges and loyalty points.

Charges up to the delegated limit (default $500) need `payment:charge`, which the agent can
obtain on its own by token exchange. Larger charges need `payment:highvalue`, which only the
MFA step-up app can issue, so the guest has to verify.

    python -m servers.payments_server                # http://127.0.0.1:8103/mcp
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from servers.guard import Requirement, ToolRule, claims_of, requires
from servers.runner import GuardSettings, run, transport_security
from servers.store import HotelStore, to_dict

DEFAULT_PORT = 8103
READ = requires("booking:read", reason="Viewing loyalty points needs the booking:read permission")
CHARGE = requires("payment:charge", "payment:highvalue", reason="Taking payments needs the payment:charge permission")
PAYMENT_METHODS = ("saved_card", "pay_at_hotel")


def create_server(
    store: HotelStore, host: str = "127.0.0.1", port: int = DEFAULT_PORT, allowed_hosts: list[str] | None = None
) -> FastMCP:
    """Build the payments server with its tools registered."""
    server = FastMCP(
        "staywise-payments",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        log_level="WARNING",
        transport_security=transport_security(port, allowed_hosts or []),
    )

    @server.tool()
    def charge_payment(
        reservation_id: str, amount: float, ctx: Context, purpose: str = "deposit", method: str = "saved_card"
    ) -> dict[str, Any]:
        """Charge the guest for a reservation. `amount` in USD; `purpose` e.g. 'deposit' or 'prepayment';
        `method` is 'saved_card' (the card on the guest's profile) or 'pay_at_hotel'."""
        if method not in PAYMENT_METHODS:
            raise ValueError(f"method must be one of {', '.join(PAYMENT_METHODS)}")
        claims = claims_of(ctx.request_context.request)
        payment = store.record_payment(
            claims.sub, reservation_id, amount, purpose, method, claims.client_id, claims.acting_agent
        )
        reservation = store.get_reservation(claims.sub, reservation_id)
        return {"payment": to_dict(payment), "reservation": to_dict(reservation)}

    @server.tool()
    def get_loyalty_balance(ctx: Context) -> dict[str, Any]:
        """The guest's StayWise loyalty points."""
        return {"points": store.loyalty_points(claims_of(ctx.request_context.request).sub)}

    @server.tool()
    def redeem_points(points: int, ctx: Context) -> dict[str, Any]:
        """Spend loyalty points (100 points = $1 off the next stay). Returns the remaining balance."""
        remaining = store.redeem_points(claims_of(ctx.request_context.request).sub, points)
        return {"redeemed": points, "remaining_points": remaining}

    return server


def create_rules(_store: HotelStore, settings: GuardSettings) -> Mapping[str, ToolRule]:
    """Charges over the limit need the MFA-only scope; everything else the delegated ones."""
    limit = settings.high_value_limit
    high_value = requires(
        "payment:highvalue",
        reason=f"Payments over ${limit:,.0f} need the guest to verify with MFA",
        client_ids=settings.stepup_client_ids,
    )

    def charge_rule(args: Mapping[str, Any]) -> Requirement:
        return high_value if float(args.get("amount", 0)) > limit else CHARGE

    return {
        "charge_payment": charge_rule,
        "get_loyalty_balance": lambda _args: READ,
        "redeem_points": lambda _args: CHARGE,
    }


def main() -> None:
    """Run the payments server."""
    run(__doc__ or "", DEFAULT_PORT, create_server, create_rules)


if __name__ == "__main__":
    main()
