import { describe, expect, it } from "vitest";
import type { ServerEvent } from "../api/types";
import { claims, ev, mfaTurn } from "../test/fixtures";
import { conciergeReducer, initialState } from "./reducer";
import {
  selectActiveStepUp,
  selectCallsForTurn,
  selectIsTurnRunning,
  selectRequestedScopes,
  selectStepUpsForCall,
  selectTimeline,
  selectTurns,
} from "./selectors";
import type { ConciergeAction, ConciergeState } from "./types";

function fold(actions: ConciergeAction[], from: ConciergeState = initialState): ConciergeState {
  return actions.reduce(conciergeReducer, from);
}

function turnOf(state: ConciergeState, turnId: string) {
  const turn = state.turns[turnId];
  if (!turn) throw new Error(`turn ${turnId} missing`);
  return turn;
}

function callOf(state: ConciergeState, callId: string) {
  const call = state.calls[callId];
  if (!call) throw new Error(`call ${callId} missing`);
  return call;
}

describe("conciergeReducer", () => {
  it("starts a turn from turn_started", () => {
    const state = fold([ev({ type: "turn_started", turnId: "t1", message: "Hello" })]);
    expect(selectTurns(state)).toHaveLength(1);
    expect(turnOf(state, "t1")).toMatchObject({ userMessage: "Hello", status: "running" });
    expect(selectIsTurnRunning(state)).toBe(true);
  });

  it("merges a local submission with the matching turn_started regardless of order", () => {
    const submitted: ConciergeAction = {
      type: "local/turn_submitted",
      turnId: "t1",
      message: "Hi",
      ts: "2026-11-01T00:00:00.000Z",
    };
    const started = ev({ type: "turn_started", turnId: "t1", message: "Hi" });
    for (const order of [
      [submitted, started],
      [started, submitted],
    ]) {
      const state = fold(order);
      expect(state.turnOrder).toEqual(["t1"]);
      expect(turnOf(state, "t1").userMessage).toBe("Hi");
    }
  });

  it("records tool calls in arrival order on the turn and the timeline", () => {
    const state = fold([
      ev({ type: "turn_started", turnId: "t1", message: "Find rooms" }),
      ev({
        type: "tool_call_started",
        turnId: "t1",
        callId: "a",
        tool: "search",
        server: "hotel",
        arguments: { city: "Galle" },
      }),
      ev({
        type: "tool_call_started",
        turnId: "t1",
        callId: "b",
        tool: "rates",
        server: "hotel",
        arguments: {},
      }),
    ]);
    expect(state.callOrder).toEqual(["a", "b"]);
    expect(turnOf(state, "t1").callIds).toEqual(["a", "b"]);
    expect(callOf(state, "a")).toMatchObject({
      tool: "search",
      server: "hotel",
      arguments: { city: "Galle" },
      status: "running",
    });
  });

  it("attaches issued tokens to the call", () => {
    const tokenClaims = claims({ scope: "rooms:read", act: { sub: "agent" } });
    const state = fold([
      ev({ type: "turn_started", turnId: "t1", message: "x" }),
      ev({
        type: "tool_call_started",
        turnId: "t1",
        callId: "a",
        tool: "search",
        server: "hotel",
        arguments: {},
      }),
      ev({
        type: "token_issued",
        turnId: "t1",
        callId: "a",
        strategy: "auto-obo",
        scopes: ["rooms:read"],
        claims: tokenClaims,
      }),
    ]);
    expect(callOf(state, "a").token).toMatchObject({
      strategy: "auto-obo",
      scopes: ["rooms:read"],
      claims: tokenClaims,
    });
  });

  it("marks calls ok or error from tool_call_finished", () => {
    const state = fold([
      ev({ type: "turn_started", turnId: "t1", message: "x" }),
      ev({
        type: "tool_call_started",
        turnId: "t1",
        callId: "a",
        tool: "one",
        server: "s",
        arguments: {},
      }),
      ev({
        type: "tool_call_started",
        turnId: "t1",
        callId: "b",
        tool: "two",
        server: "s",
        arguments: {},
      }),
      ev({
        type: "tool_call_finished",
        turnId: "t1",
        callId: "a",
        ok: true,
        resultPreview: "3 rooms",
      }),
      ev({
        type: "tool_call_finished",
        turnId: "t1",
        callId: "b",
        ok: false,
        resultPreview: "",
        error: "denied",
      }),
    ]);
    expect(callOf(state, "a")).toMatchObject({ status: "ok", resultPreview: "3 rooms" });
    expect(callOf(state, "b")).toMatchObject({ status: "error", error: "denied" });
  });

  it("appends assistant messages with unique ids and finishes the turn", () => {
    const state = fold([
      ev({ type: "turn_started", turnId: "t1", message: "x" }),
      ev({ type: "assistant_message", turnId: "t1", content: "first" }),
      ev({ type: "assistant_message", turnId: "t1", content: "second" }),
      ev({ type: "turn_finished", turnId: "t1" }),
    ]);
    const turn = turnOf(state, "t1");
    expect(turn.replies.map((r) => r.content)).toEqual(["first", "second"]);
    expect(new Set(turn.replies.map((r) => r.id)).size).toBe(2);
    expect(turn.status).toBe("finished");
    expect(selectIsTurnRunning(state)).toBe(false);
  });

  it("records turn_failed with its error and keeps it failed after turn_finished", () => {
    const state = fold([
      ev({ type: "turn_started", turnId: "t1", message: "x" }),
      ev({ type: "turn_failed", turnId: "t1", error: "LLM unavailable" }),
      ev({ type: "turn_finished", turnId: "t1" }),
    ]);
    expect(turnOf(state, "t1")).toMatchObject({ status: "failed", error: "LLM unavailable" });
  });

  it("creates placeholders when events arrive before their parents", () => {
    const state = fold([
      ev({
        type: "token_issued",
        turnId: "t9",
        callId: "z",
        strategy: "cached",
        scopes: ["a"],
        claims: claims(),
      }),
      ev({
        type: "tool_call_started",
        turnId: "t9",
        callId: "z",
        tool: "late",
        server: "srv",
        arguments: {},
      }),
      ev({ type: "turn_started", turnId: "t9", message: "late start" }),
    ]);
    expect(state.turnOrder).toEqual(["t9"]);
    expect(turnOf(state, "t9").userMessage).toBe("late start");
    expect(callOf(state, "z")).toMatchObject({ tool: "late", server: "srv" });
    expect(callOf(state, "z").token?.strategy).toBe("cached");
  });

  it("does not mutate the previous state", () => {
    const before = fold([ev({ type: "turn_started", turnId: "t1", message: "x" })]);
    const snapshot = structuredClone(before);
    conciergeReducer(
      before,
      ev({
        type: "tool_call_started",
        turnId: "t1",
        callId: "a",
        tool: "t",
        server: "s",
        arguments: {},
      }),
    );
    conciergeReducer(before, ev({ type: "assistant_message", turnId: "t1", content: "hi" }));
    expect(before).toEqual(snapshot);
  });

  describe("step-up lifecycle", () => {
    const events = mfaTurn();
    const upTo = (type: ServerEvent["type"]) =>
      events.slice(0, events.findIndex((e) => e.type === type) + 1);

    it("tracks a pending step-up as the active one", () => {
      const state = fold(upTo("stepup_required"));
      const active = selectActiveStepUp(state);
      expect(active).toMatchObject({
        stepupId: "s1",
        callId: "c1",
        status: "pending",
        scope: ["payments:charge"],
        startUrl: "/auth/stepup/s1",
      });
      expect(callOf(state, "c1").stepUpIds).toEqual(["s1"]);
      expect(selectRequestedScopes(state, callOf(state, "c1"))).toEqual(["payments:charge"]);
    });

    it("resolves the step-up as approved and clears the active one", () => {
      const state = fold(upTo("stepup_resolved"));
      expect(state.stepUps.s1?.status).toBe("approved");
      expect(state.stepUps.s1?.resolvedAt).toBeDefined();
      expect(selectActiveStepUp(state)).toBeUndefined();
    });

    it.each(["declined", "timeout"] as const)("records a %s outcome", (outcome) => {
      const state = fold([
        ...upTo("stepup_required"),
        ev({ type: "stepup_resolved", turnId: "t1", stepupId: "s1", outcome }),
      ]);
      expect(state.stepUps.s1?.status).toBe(outcome);
      expect(selectActiveStepUp(state)).toBeUndefined();
    });

    it("ignores a second resolution and unknown step-ups", () => {
      const resolved = fold(upTo("stepup_resolved"));
      const again = conciergeReducer(
        resolved,
        ev({ type: "stepup_resolved", turnId: "t1", stepupId: "s1", outcome: "declined" }),
      );
      expect(again.stepUps.s1?.status).toBe("approved");
      const unknown = conciergeReducer(
        resolved,
        ev({ type: "stepup_resolved", turnId: "t1", stepupId: "nope", outcome: "approved" }),
      );
      expect(unknown).toBe(resolved);
    });

    it("drops the active step-up when its turn fails", () => {
      const state = fold([
        ...upTo("stepup_required"),
        ev({ type: "turn_failed", turnId: "t1", error: "boom" }),
      ]);
      expect(state.stepUps.s1?.status).toBe("pending");
      expect(selectActiveStepUp(state)).toBeUndefined();
    });

    it("folds the full MFA turn into a single successful call", () => {
      const state = fold(events);
      const [call] = selectTimeline(state);
      expect(call).toMatchObject({ status: "ok", token: { strategy: "user-mfa" } });
      expect(call && selectStepUpsForCall(state, call).map((s) => s.status)).toEqual(["approved"]);
      expect(selectCallsForTurn(state, turnOf(state, "t1"))).toHaveLength(1);
      expect(turnOf(state, "t1").status).toBe("finished");
    });

    it("picks the newest pending step-up when several are open", () => {
      const state = fold([
        ...upTo("stepup_required"),
        ev({
          type: "stepup_required",
          turnId: "t1",
          callId: "c2",
          stepupId: "s2",
          tool: "upgrade",
          scope: ["bookings:write"],
          reason: "",
          startUrl: "/auth/stepup/s2",
          expiresAt: new Date(Date.now() + 60_000).toISOString(),
        }),
      ]);
      expect(selectActiveStepUp(state)?.stepupId).toBe("s2");
    });
  });

  describe("phone messages", () => {
    it("loads, dedupes, and sorts by receivedAt; live messages are flagged", () => {
      const loaded = conciergeReducer(initialState, {
        type: "local/phone_loaded",
        messages: [
          { id: "m2", receivedAt: "2026-11-01T10:02:00Z", text: "second" },
          { id: "m1", receivedAt: "2026-11-01T10:01:00Z", text: "first" },
        ],
      });
      const live = fold(
        [
          ev({
            type: "phone_message",
            id: "m3",
            receivedAt: "2026-11-01T10:03:00Z",
            text: "Code 123456",
            otp: "123456",
          }),
          ev({ type: "phone_message", id: "m3", receivedAt: "2026-11-01T10:03:00Z", text: "dup" }),
          {
            type: "local/phone_loaded",
            messages: [{ id: "m1", receivedAt: "2026-11-01T10:01:00Z", text: "first" }],
          },
        ],
        loaded,
      );
      expect(live.phone.map((m) => m.id)).toEqual(["m1", "m2", "m3"]);
      expect(live.phone.map((m) => m.live)).toEqual([false, false, true]);
      expect(live.phone[2]).toMatchObject({ otp: "123456", text: "Code 123456" });
    });
  });

  describe("reset", () => {
    it("clears turns, calls and step-ups but keeps phone messages", () => {
      const before = fold([
        ...mfaTurn(),
        ev({ type: "phone_message", id: "m1", receivedAt: "2026-11-01T10:00:00Z", text: "hi" }),
      ]);
      const after = conciergeReducer(before, { type: "local/reset" });
      expect(after.turnOrder).toEqual([]);
      expect(after.callOrder).toEqual([]);
      expect(after.stepUps).toEqual({});
      expect(after.phone).toHaveLength(1);
    });

    it("ignores late events for turns that were reset, but accepts new turns", () => {
      const reset = fold([
        ev({ type: "turn_started", turnId: "t1", message: "old" }),
        { type: "local/reset" },
      ]);
      const late = fold(
        [
          ev({ type: "assistant_message", turnId: "t1", content: "stale" }),
          ev({
            type: "tool_call_started",
            turnId: "t1",
            callId: "a",
            tool: "x",
            server: "s",
            arguments: {},
          }),
          ev({ type: "turn_started", turnId: "t2", message: "new" }),
        ],
        reset,
      );
      expect(late.turnOrder).toEqual(["t2"]);
      expect(late.callOrder).toEqual([]);
    });
  });
});
