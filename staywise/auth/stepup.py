"""Interactive step-up: pauses a tool call until the user finishes MFA in a popup, declines, or times out."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from staywise.auth.thunder import Pkce, TokenSet
from staywise.events import CallInfo, EventSink


class Outcome(StrEnum):
    """How an interactive step-up ended."""

    APPROVED = "approved"
    DECLINED = "declined"
    TIMEOUT = "timeout"


class StepUpRefusedError(Exception):
    """A stronger token could not be obtained; `message` is safe to show the model and the user."""

    def __init__(self, message: str, outcome: str = "unavailable") -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass
class PendingStepUp:
    """One step-up waiting for the user. `state`, `nonce` and `pkce` secure the popup's OAuth round trip."""

    id: str
    scopes: frozenset[str]
    reason: str
    expires_at: float
    call: CallInfo | None
    state: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    pkce: Pkce = field(default_factory=Pkce.new)
    started: bool = False
    future: asyncio.Future[TokenSet] = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class StepUpCoordinator:
    """Tracks one chat session's pending step-ups and announces them on its event stream."""

    def __init__(
        self,
        emit: EventSink,
        timeout_seconds: float,
        clock: Callable[[], float] = time.time,
        *,
        start_path: str = "/auth/stepup/start",
    ) -> None:
        self._emit = emit
        self._start_path = start_path
        self._timeout = timeout_seconds
        self._clock = clock
        self._pending: dict[str, PendingStepUp] = {}

    async def request(self, scopes: frozenset[str], reason: str, call: CallInfo | None) -> TokenSet:
        """Ask the user to verify, and wait for the result.

        Raises:
            StepUpRefusedError: if the user declines or does not finish in time.
        """
        stepup = PendingStepUp(
            id=secrets.token_urlsafe(12),
            scopes=scopes,
            reason=reason,
            expires_at=self._clock() + self._timeout,
            call=call,
        )
        self._pending[stepup.id] = stepup
        self._emit(
            "stepup_required",
            {
                "turnId": call.turn_id if call else None,
                "callId": call.call_id if call else None,
                "stepupId": stepup.id,
                "tool": call.tool if call else None,
                "scope": sorted(scopes),
                "reason": reason,
                "startUrl": f"{self._start_path}/{stepup.id}",
                "expiresAt": datetime.fromtimestamp(stepup.expires_at, UTC).isoformat(),
            },
        )
        try:
            token = await asyncio.wait_for(asyncio.shield(stepup.future), timeout=self._timeout)
        except TimeoutError:
            self._finish(stepup, Outcome.TIMEOUT)
            raise StepUpRefusedError(
                "the guest did not complete the extra verification in time", Outcome.TIMEOUT
            ) from None
        finally:
            self._pending.pop(stepup.id, None)
        return token

    def get(self, stepup_id: str) -> PendingStepUp | None:
        """A pending step-up by id, if it has not expired."""
        stepup = self._pending.get(stepup_id)
        return stepup if stepup and stepup.expires_at > self._clock() else None

    def by_state(self, state: str) -> PendingStepUp | None:
        """The pending step-up whose OAuth `state` this is."""
        return next((s for s in self._pending.values() if secrets.compare_digest(s.state, state)), None)

    def approve(self, stepup: PendingStepUp, token: TokenSet) -> None:
        """Resume the paused tool call with the stepped-up token."""
        if not stepup.future.done():
            stepup.future.set_result(token)
            self._finish(stepup, Outcome.APPROVED)

    def decline(self, stepup: PendingStepUp, message: str = "the guest declined the extra verification") -> None:
        """Fail the paused tool call."""
        if not stepup.future.done():
            stepup.future.set_exception(StepUpRefusedError(message, Outcome.DECLINED))
            self._finish(stepup, Outcome.DECLINED)

    def cancel_all(self) -> None:
        """Decline everything pending (the session is ending)."""
        for stepup in list(self._pending.values()):
            self.decline(stepup, "the session ended")

    def _finish(self, stepup: PendingStepUp, outcome: Outcome) -> None:
        self._emit(
            "stepup_resolved",
            {
                "turnId": stepup.call.turn_id if stepup.call else None,
                "stepupId": stepup.id,
                "outcome": outcome.value,
            },
        )
