import { SERVER_EVENT_TYPES, type ServerEvent, type ServerEventType } from "./types";

const REQUIRED_STRING_FIELDS: Record<ServerEventType, readonly string[]> = {
  turn_started: ["turnId", "message"],
  tool_call_started: ["turnId", "callId", "tool"],
  token_issued: ["turnId", "callId", "strategy"],
  stepup_required: ["turnId", "callId", "stepupId", "startUrl"],
  stepup_resolved: ["turnId", "stepupId", "outcome"],
  tool_call_finished: ["turnId", "callId"],
  assistant_message: ["turnId", "content"],
  turn_finished: ["turnId"],
  turn_failed: ["turnId"],
  phone_message: ["id", "text"],
};

export function isServerEventType(name: string): name is ServerEventType {
  return (SERVER_EVENT_TYPES as readonly string[]).includes(name);
}

/**
 * Parses one SSE frame into a typed event. The SSE `event:` name becomes the
 * discriminant. Returns null for unknown names, malformed JSON, or payloads
 * missing the identifiers the reducer relies on.
 */
export function parseServerEvent(name: string, data: string): ServerEvent | null {
  if (!isServerEventType(name)) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  const valid = REQUIRED_STRING_FIELDS[name].every((field) => typeof record[field] === "string");
  if (!valid) return null;
  const ts = typeof record.ts === "string" ? record.ts : new Date().toISOString();
  return { ...record, ts, type: name } as ServerEvent;
}
