import type { ServerEvent, TokenClaims } from "../api/types";

let clock = Date.parse("2026-11-01T10:00:00.000Z");

/** Monotonic ISO timestamps so ordering assertions are deterministic. */
export function nextTs(): string {
  clock += 1000;
  return new Date(clock).toISOString();
}

export function claims(overrides: Partial<TokenClaims> = {}): TokenClaims {
  return {
    sub: "alice",
    client_id: "staywise-agent",
    scope: "rooms:read",
    aud: "hotel-api",
    exp: Math.floor(Date.now() / 1000) + 300,
    iat: Math.floor(Date.now() / 1000),
    ...overrides,
  };
}

type Without<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;
export type EventInput = Without<ServerEvent, "ts"> & { ts?: string };

export function ev(input: EventInput): ServerEvent {
  return { ts: nextTs(), ...input };
}

/** A complete user-MFA turn: start → tool → step-up → approved → token → finish. */
export function mfaTurn(turnId = "t1", callId = "c1", stepupId = "s1"): ServerEvent[] {
  return [
    ev({ type: "turn_started", turnId, message: "Pay the deposit" }),
    ev({
      type: "tool_call_started",
      turnId,
      callId,
      tool: "charge_card",
      server: "payments",
      arguments: { amount: 90 },
    }),
    ev({
      type: "stepup_required",
      turnId,
      callId,
      stepupId,
      tool: "charge_card",
      scope: ["payments:charge"],
      reason: "Payments need MFA",
      startUrl: `/auth/stepup/${stepupId}`,
      expiresAt: new Date(Date.now() + 120_000).toISOString(),
    }),
    ev({ type: "stepup_resolved", turnId, stepupId, outcome: "approved" }),
    ev({
      type: "token_issued",
      turnId,
      callId,
      strategy: "user-mfa",
      scopes: ["payments:charge"],
      claims: claims({ scope: "payments:charge", act: { sub: "staywise-agent" } }),
    }),
    ev({ type: "tool_call_finished", turnId, callId, ok: true, resultPreview: "charged $90" }),
    ev({ type: "assistant_message", turnId, content: "Done! **$90** charged." }),
    ev({ type: "turn_finished", turnId }),
  ];
}

type Listener = (event: MessageEvent<string> | Event) => void;

/** Minimal in-memory EventSource for tests. */
export class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly url: string;
  readyState = 0;
  closed = false;
  private readonly listeners = new Map<string, Set<Listener>>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  static reset(): void {
    MockEventSource.instances = [];
  }

  static latest(): MockEventSource {
    const last = MockEventSource.instances.at(-1);
    if (!last) throw new Error("no EventSource was created");
    return last;
  }

  addEventListener(type: string, listener: Listener): void {
    const set = this.listeners.get(type) ?? new Set<Listener>();
    set.add(listener);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  listenerCount(type: string): number {
    return this.listeners.get(type)?.size ?? 0;
  }

  open(): void {
    this.readyState = 1;
    this.fire("open", new Event("open"));
  }

  error(closed = false): void {
    this.readyState = closed ? 2 : 0;
    this.fire("error", new Event("error"));
  }

  /** Emits a named SSE event whose data is the JSON payload (minus `type`). */
  emit(event: ServerEvent): void {
    const { type, ...payload } = event;
    this.emitRaw(type, JSON.stringify(payload));
  }

  emitRaw(type: string, data: string): void {
    this.fire(type, new MessageEvent<string>(type, { data }));
  }

  private fire(type: string, event: MessageEvent<string> | Event): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  asEventSource(): EventSource {
    return this as unknown as EventSource;
  }
}

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function noContent(): Response {
  return new Response(null, { status: 204 });
}
