import { describe, expect, it } from "vitest";
import { parseServerEvent } from "./events";

describe("parseServerEvent", () => {
  it("injects the SSE event name as the discriminant", () => {
    const event = parseServerEvent(
      "turn_started",
      JSON.stringify({ turnId: "t1", message: "hi", ts: "2026-11-01T00:00:00Z" }),
    );
    expect(event).toEqual({
      type: "turn_started",
      turnId: "t1",
      message: "hi",
      ts: "2026-11-01T00:00:00Z",
    });
  });

  it("fills a missing ts", () => {
    const event = parseServerEvent("turn_finished", JSON.stringify({ turnId: "t1" }));
    expect(event?.ts).toEqual(expect.any(String));
  });

  it.each([
    ["unknown event name", "something_else", JSON.stringify({ turnId: "t1" })],
    ["malformed JSON", "turn_started", "{nope"],
    ["non-object payload", "turn_started", "[]"],
    ["missing identifiers", "tool_call_started", JSON.stringify({ turnId: "t1" })],
    ["phone message without id", "phone_message", JSON.stringify({ text: "x" })],
  ])("rejects %s", (_label, name, data) => {
    expect(parseServerEvent(name, data)).toBeNull();
  });
});
