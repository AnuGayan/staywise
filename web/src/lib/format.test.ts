import { describe, expect, it } from "vitest";
import {
  actorChain,
  delegationLabel,
  formatAudience,
  formatRemaining,
  splitScopes,
} from "./format";

describe("format helpers", () => {
  it("renders the act delegation chain as user → agent", () => {
    expect(delegationLabel("alice", { sub: "staywise-agent" })).toBe("alice → staywise-agent");
    expect(delegationLabel("alice", { sub: "booking-agent", act: { sub: "concierge" } })).toBe(
      "alice → booking-agent → concierge",
    );
    expect(delegationLabel("alice", undefined)).toBeNull();
    expect(actorChain(undefined)).toEqual([]);
  });

  it.each([
    [0, "expired"],
    [-5, "expired"],
    [59_001, "1m 00s"],
    [59_000, "59s"],
    [125_000, "2m 05s"],
    [3_725_000, "1h 02m"],
  ])("formats %d ms as %s", (ms, text) => {
    expect(formatRemaining(ms)).toBe(text);
  });

  it("formats audiences and scope strings", () => {
    expect(formatAudience(["a", "b"])).toBe("a, b");
    expect(formatAudience("a")).toBe("a");
    expect(splitScopes(" rooms:read  payments:charge ")).toEqual(["rooms:read", "payments:charge"]);
  });
});
