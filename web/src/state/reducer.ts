import type { PhoneMessage } from "../api/types";
import type { ConciergeAction, ConciergeState, PhoneEntry, StepUp, ToolCall, Turn } from "./types";

export const initialState: ConciergeState = {
  turnOrder: [],
  turns: {},
  callOrder: [],
  calls: {},
  stepUps: {},
  phone: [],
  discardedTurnIds: [],
};

function appendUnique(list: readonly string[], id: string): string[] {
  return list.includes(id) ? [...list] : [...list, id];
}

/** Returns state with the turn present (creating a placeholder if needed). */
function ensureTurn(state: ConciergeState, turnId: string, ts: string): ConciergeState {
  if (state.turns[turnId]) return state;
  const turn: Turn = {
    turnId,
    userMessage: "",
    startedAt: ts,
    status: "running",
    callIds: [],
    replies: [],
  };
  return {
    ...state,
    turnOrder: [...state.turnOrder, turnId],
    turns: { ...state.turns, [turnId]: turn },
  };
}

function updateTurn(
  state: ConciergeState,
  turnId: string,
  ts: string,
  patch: (turn: Turn) => Turn,
): ConciergeState {
  const withTurn = ensureTurn(state, turnId, ts);
  const turn = withTurn.turns[turnId];
  if (!turn) return withTurn;
  return { ...withTurn, turns: { ...withTurn.turns, [turnId]: patch(turn) } };
}

interface CallSeed {
  turnId: string;
  callId: string;
  ts: string;
  tool?: string;
}

/** Returns state with the call present and linked to its turn and the timeline. */
function ensureCall(state: ConciergeState, seed: CallSeed): ConciergeState {
  const withTurn = updateTurn(state, seed.turnId, seed.ts, (turn) => ({
    ...turn,
    callIds: appendUnique(turn.callIds, seed.callId),
  }));
  if (withTurn.calls[seed.callId]) return withTurn;
  const call: ToolCall = {
    callId: seed.callId,
    turnId: seed.turnId,
    tool: seed.tool ?? "tool",
    server: "",
    arguments: {},
    startedAt: seed.ts,
    status: "running",
    stepUpIds: [],
  };
  return {
    ...withTurn,
    callOrder: appendUnique(withTurn.callOrder, seed.callId),
    calls: { ...withTurn.calls, [seed.callId]: call },
  };
}

function updateCall(
  state: ConciergeState,
  seed: CallSeed,
  patch: (call: ToolCall) => ToolCall,
): ConciergeState {
  const withCall = ensureCall(state, seed);
  const call = withCall.calls[seed.callId];
  if (!call) return withCall;
  return { ...withCall, calls: { ...withCall.calls, [seed.callId]: patch(call) } };
}

function byReceivedAt(a: PhoneEntry, b: PhoneEntry): number {
  return a.receivedAt.localeCompare(b.receivedAt);
}

function mergePhone(
  existing: readonly PhoneEntry[],
  incoming: readonly PhoneMessage[],
  live: boolean,
): PhoneEntry[] {
  const known = new Set(existing.map((m) => m.id));
  const added = incoming
    .filter((m) => !known.has(m.id))
    .map((m): PhoneEntry => ({
      id: m.id,
      receivedAt: m.receivedAt,
      text: m.text,
      otp: m.otp,
      live,
    }));
  if (added.length === 0) return [...existing];
  return [...existing, ...added].sort(byReceivedAt);
}

function isDiscarded(state: ConciergeState, action: ConciergeAction): boolean {
  return "turnId" in action && state.discardedTurnIds.includes(action.turnId);
}

/** Pure reducer folding backend events and local UI actions into concierge state. */
export function conciergeReducer(state: ConciergeState, action: ConciergeAction): ConciergeState {
  if (isDiscarded(state, action)) return state;

  switch (action.type) {
    case "local/turn_submitted":
    case "turn_started":
      return updateTurn(state, action.turnId, action.ts, (turn) => ({
        ...turn,
        userMessage: turn.userMessage.length > 0 ? turn.userMessage : action.message,
        startedAt: turn.userMessage.length > 0 ? turn.startedAt : action.ts,
      }));

    case "tool_call_started":
      return updateCall(state, action, (call) => ({
        ...call,
        tool: action.tool,
        server: action.server,
        arguments: action.arguments,
        startedAt: action.ts,
      }));

    case "token_issued":
      return updateCall(state, action, (call) => ({
        ...call,
        token: {
          strategy: action.strategy,
          scopes: [...action.scopes],
          claims: action.claims,
          issuedAt: action.ts,
        },
      }));

    case "stepup_required": {
      const stepUp: StepUp = {
        stepupId: action.stepupId,
        callId: action.callId,
        turnId: action.turnId,
        tool: action.tool,
        scope: [...action.scope],
        reason: action.reason,
        startUrl: action.startUrl,
        expiresAt: action.expiresAt,
        requestedAt: action.ts,
        status: "pending",
      };
      const withCall = updateCall(state, action, (call) => ({
        ...call,
        stepUpIds: appendUnique(call.stepUpIds, action.stepupId),
      }));
      return { ...withCall, stepUps: { ...withCall.stepUps, [action.stepupId]: stepUp } };
    }

    case "stepup_resolved": {
      const stepUp = state.stepUps[action.stepupId];
      if (!stepUp || stepUp.status !== "pending") return state;
      return {
        ...state,
        stepUps: {
          ...state.stepUps,
          [action.stepupId]: { ...stepUp, status: action.outcome, resolvedAt: action.ts },
        },
      };
    }

    case "tool_call_finished":
      return updateCall(state, action, (call) => ({
        ...call,
        status: action.ok ? "ok" : "error",
        resultPreview: action.resultPreview,
        error: action.error,
        finishedAt: action.ts,
      }));

    case "assistant_message":
      return updateTurn(state, action.turnId, action.ts, (turn) => ({
        ...turn,
        replies: [
          ...turn.replies,
          {
            id: `${turn.turnId}:reply-${turn.replies.length + 1}`,
            content: action.content,
            ts: action.ts,
          },
        ],
      }));

    case "turn_finished":
      return updateTurn(state, action.turnId, action.ts, (turn) => ({
        ...turn,
        status: turn.status === "failed" ? "failed" : "finished",
        finishedAt: action.ts,
      }));

    case "turn_failed":
      return updateTurn(state, action.turnId, action.ts, (turn) => ({
        ...turn,
        status: "failed",
        error: action.error,
        finishedAt: action.ts,
      }));

    case "phone_message":
      return { ...state, phone: mergePhone(state.phone, [action], true) };

    case "local/phone_loaded":
      return { ...state, phone: mergePhone(state.phone, action.messages, false) };

    case "local/reset":
      return {
        ...initialState,
        phone: state.phone,
        discardedTurnIds: [...state.discardedTurnIds, ...state.turnOrder],
      };

    default:
      return state;
  }
}
