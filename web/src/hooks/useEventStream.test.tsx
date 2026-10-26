import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ServerEvent } from "../api/types";
import { MockEventSource, ev } from "../test/fixtures";
import { useEventStream } from "./useEventStream";

function createSource(url: string): EventSource {
  return new MockEventSource(url).asEventSource();
}

afterEach(() => {
  MockEventSource.reset();
});

describe("useEventStream", () => {
  it("opens one EventSource and reports connection state", () => {
    const onEvent = vi.fn();
    const { result } = renderHook(() => useEventStream("/api/events", onEvent, { createSource }));
    expect(MockEventSource.instances).toHaveLength(1);
    expect(MockEventSource.latest().url).toBe("/api/events");
    expect(result.current).toBe("connecting");

    act(() => {
      MockEventSource.latest().open();
    });
    expect(result.current).toBe("open");

    act(() => {
      MockEventSource.latest().error();
    });
    expect(result.current).toBe("reconnecting");

    act(() => {
      MockEventSource.latest().open();
    });
    expect(result.current).toBe("open");

    act(() => {
      MockEventSource.latest().error(true);
    });
    expect(result.current).toBe("closed");
  });

  it("delivers parsed, typed events and skips malformed frames", () => {
    const received: ServerEvent[] = [];
    renderHook(() =>
      useEventStream(
        "/api/events",
        (e) => {
          received.push(e);
        },
        { createSource },
      ),
    );
    const source = MockEventSource.latest();
    act(() => {
      source.emit(ev({ type: "turn_started", turnId: "t1", message: "hi" }));
      source.emitRaw("tool_call_started", "{broken");
      source.emit(
        ev({
          type: "phone_message",
          id: "m1",
          receivedAt: "2026-11-01T00:00:00Z",
          text: "x",
          otp: "1",
        }),
      );
    });
    expect(received.map((e) => e.type)).toEqual(["turn_started", "phone_message"]);
    expect(received[0]).toMatchObject({ turnId: "t1", message: "hi" });
  });

  it("uses the latest handler without reopening the stream", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(
      ({ handler }) => useEventStream("/api/events", handler, { createSource }),
      {
        initialProps: { handler: first },
      },
    );
    rerender({ handler: second });
    act(() => {
      MockEventSource.latest().emit(ev({ type: "turn_finished", turnId: "t1" }));
    });
    expect(MockEventSource.instances).toHaveLength(1);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("closes the stream and removes listeners on unmount", () => {
    const { unmount } = renderHook(() => useEventStream("/api/events", vi.fn(), { createSource }));
    const source = MockEventSource.latest();
    expect(source.listenerCount("turn_started")).toBe(1);
    unmount();
    expect(source.closed).toBe(true);
    expect(source.listenerCount("turn_started")).toBe(0);
  });

  it("does not connect while disabled", () => {
    renderHook(() => useEventStream("/api/events", vi.fn(), { createSource, enabled: false }));
    expect(MockEventSource.instances).toHaveLength(0);
  });

  it("uses the global EventSource by default", () => {
    vi.stubGlobal("EventSource", MockEventSource);
    renderHook(() => useEventStream("/api/events", vi.fn()));
    expect(MockEventSource.latest().url).toBe("/api/events");
  });
});
