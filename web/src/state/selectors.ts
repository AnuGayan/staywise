import type { ConciergeState, StepUp, ToolCall, Turn } from "./types";

function isPresent<T>(value: T | undefined): value is T {
  return value !== undefined;
}

export function selectTurns(state: ConciergeState): Turn[] {
  return state.turnOrder.map((id) => state.turns[id]).filter(isPresent);
}

export function selectTimeline(state: ConciergeState): ToolCall[] {
  return state.callOrder.map((id) => state.calls[id]).filter(isPresent);
}

export function selectCallsForTurn(state: ConciergeState, turn: Turn): ToolCall[] {
  return turn.callIds.map((id) => state.calls[id]).filter(isPresent);
}

export function selectStepUpsForCall(state: ConciergeState, call: ToolCall): StepUp[] {
  return call.stepUpIds.map((id) => state.stepUps[id]).filter(isPresent);
}

export function selectIsTurnRunning(state: ConciergeState): boolean {
  return state.turnOrder.some((id) => state.turns[id]?.status === "running");
}

export function selectRunningTurn(state: ConciergeState): Turn | undefined {
  return selectTurns(state).find((turn) => turn.status === "running");
}

/** The newest pending step-up whose turn is still running, if any. */
export function selectActiveStepUp(state: ConciergeState): StepUp | undefined {
  return Object.values(state.stepUps)
    .filter((s) => s.status === "pending" && state.turns[s.turnId]?.status === "running")
    .sort((a, b) => b.requestedAt.localeCompare(a.requestedAt))[0];
}

/** Scopes the backend asked for during step-up (union, in first-seen order). */
export function selectRequestedScopes(state: ConciergeState, call: ToolCall): string[] {
  const scopes = selectStepUpsForCall(state, call).flatMap((s) => s.scope);
  return [...new Set(scopes)];
}
