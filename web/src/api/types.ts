/** Shapes of the StayWise backend contract (HTTP + SSE). */

export interface User {
  sub: string;
  name: string;
}

export interface MeAuthenticated {
  authenticated: true;
  user: User;
  grantedScopes: string[];
}

export interface MeUnauthenticated {
  authenticated: false;
  loginUrl: string;
}

export type MeResponse = MeAuthenticated | MeUnauthenticated;

export type TokenStrategy = "cached" | "auto-obo" | "auto-agent" | "user-mfa";

export type StepUpOutcome = "approved" | "declined" | "timeout";

/** RFC 8693 actor claim; may nest for multi-hop delegation. */
export interface ActClaim {
  sub: string;
  act?: ActClaim;
}

export interface TokenClaims {
  sub: string;
  client_id: string;
  scope: string;
  aud: string | string[];
  exp: number;
  iat: number;
  jti?: string;
  act?: ActClaim;
}

export interface PhoneMessage {
  id: string;
  receivedAt: string;
  text: string;
  otp?: string;
}

interface BaseEvent {
  ts: string;
}

export interface TurnStartedEvent extends BaseEvent {
  type: "turn_started";
  turnId: string;
  message: string;
}

export interface ToolCallStartedEvent extends BaseEvent {
  type: "tool_call_started";
  turnId: string;
  callId: string;
  tool: string;
  server: string;
  arguments: Record<string, unknown>;
}

export interface TokenIssuedEvent extends BaseEvent {
  type: "token_issued";
  turnId: string;
  callId: string;
  strategy: TokenStrategy;
  scopes: string[];
  claims: TokenClaims;
}

export interface StepUpRequiredEvent extends BaseEvent {
  type: "stepup_required";
  turnId: string;
  callId: string;
  stepupId: string;
  tool: string;
  scope: string[];
  reason: string;
  startUrl: string;
  expiresAt: string;
}

export interface StepUpResolvedEvent extends BaseEvent {
  type: "stepup_resolved";
  turnId: string;
  stepupId: string;
  outcome: StepUpOutcome;
}

export interface ToolCallFinishedEvent extends BaseEvent {
  type: "tool_call_finished";
  turnId: string;
  callId: string;
  ok: boolean;
  resultPreview: string;
  error?: string;
}

export interface AssistantMessageEvent extends BaseEvent {
  type: "assistant_message";
  turnId: string;
  content: string;
}

export interface TurnFinishedEvent extends BaseEvent {
  type: "turn_finished";
  turnId: string;
}

export interface TurnFailedEvent extends BaseEvent {
  type: "turn_failed";
  turnId: string;
  error: string;
}

export interface PhoneMessageEvent extends BaseEvent, PhoneMessage {
  type: "phone_message";
}

/**
 * Discriminated union of every SSE event. The `type` field is not on the wire;
 * it is injected from the SSE `event:` name when the event is parsed.
 */
export type ServerEvent =
  | TurnStartedEvent
  | ToolCallStartedEvent
  | TokenIssuedEvent
  | StepUpRequiredEvent
  | StepUpResolvedEvent
  | ToolCallFinishedEvent
  | AssistantMessageEvent
  | TurnFinishedEvent
  | TurnFailedEvent
  | PhoneMessageEvent;

export type ServerEventType = ServerEvent["type"];

export const SERVER_EVENT_TYPES: readonly ServerEventType[] = [
  "turn_started",
  "tool_call_started",
  "token_issued",
  "stepup_required",
  "stepup_resolved",
  "tool_call_finished",
  "assistant_message",
  "turn_finished",
  "turn_failed",
  "phone_message",
];
