import type {
  PhoneMessage,
  ServerEvent,
  StepUpOutcome,
  TokenClaims,
  TokenStrategy,
} from "../api/types";

export type TurnStatus = "running" | "finished" | "failed";
export type ToolCallStatus = "running" | "ok" | "error";
export type StepUpStatus = "pending" | StepUpOutcome;

export interface AssistantReply {
  id: string;
  content: string;
  ts: string;
}

export interface Turn {
  turnId: string;
  userMessage: string;
  startedAt: string;
  status: TurnStatus;
  callIds: string[];
  replies: AssistantReply[];
  error?: string;
  finishedAt?: string;
}

export interface IssuedToken {
  strategy: TokenStrategy;
  scopes: string[];
  claims: TokenClaims;
  issuedAt: string;
}

export interface ToolCall {
  callId: string;
  turnId: string;
  tool: string;
  server: string;
  arguments: Record<string, unknown>;
  startedAt: string;
  status: ToolCallStatus;
  token?: IssuedToken;
  stepUpIds: string[];
  resultPreview?: string;
  error?: string;
  finishedAt?: string;
}

export interface StepUp {
  stepupId: string;
  callId: string;
  turnId: string;
  tool: string;
  scope: string[];
  reason: string;
  startUrl: string;
  expiresAt: string;
  requestedAt: string;
  status: StepUpStatus;
  resolvedAt?: string;
}

export interface PhoneEntry extends PhoneMessage {
  /** True when the message arrived live over SSE (drives the highlight). */
  live: boolean;
}

export interface ConciergeState {
  turnOrder: string[];
  turns: Record<string, Turn>;
  callOrder: string[];
  calls: Record<string, ToolCall>;
  stepUps: Record<string, StepUp>;
  phone: PhoneEntry[];
  /** Turns cleared by a reset; late events for them are ignored. */
  discardedTurnIds: string[];
}

export type LocalAction =
  | { type: "local/turn_submitted"; turnId: string; message: string; ts: string }
  | { type: "local/reset" }
  | { type: "local/phone_loaded"; messages: PhoneMessage[] };

export type ConciergeAction = ServerEvent | LocalAction;
