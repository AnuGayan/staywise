import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { MockEventSource, claims, ev, jsonResponse } from "./test/fixtures";
import { mockFetch } from "./test/fetchMock";

const ME = {
  authenticated: true,
  user: { sub: "alice", name: "Alice Perera" },
  grantedScopes: ["openid", "rooms:read"],
};

afterEach(() => {
  MockEventSource.reset();
});

describe("App", () => {
  it("shows the sign-in landing page on 401 with a link to loginUrl", async () => {
    mockFetch({
      "GET api/me": () =>
        jsonResponse(401, { authenticated: false, loginUrl: "/auth/login?next=%2F" }),
    });
    vi.stubGlobal("EventSource", MockEventSource);
    render(<App />);
    const link = await screen.findByRole("link", { name: "Sign in to start" });
    expect(link).toHaveAttribute("href", "/auth/login?next=%2F");
    expect(screen.getByRole("heading", { name: "StayWise Concierge" })).toBeInTheDocument();
    expect(MockEventSource.instances).toHaveLength(0);
  });

  it("offers a retry when the session cannot be loaded", async () => {
    const user = userEvent.setup();
    let calls = 0;
    mockFetch({
      "GET api/me": () => {
        calls += 1;
        return calls === 1
          ? jsonResponse(500, { error: "Backend unavailable" })
          : jsonResponse(401, { authenticated: false, loginUrl: "/auth/login" });
      },
    });
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Backend unavailable");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "Sign in to start" })).toHaveAttribute(
      "href",
      "/auth/login",
    );
  });

  it("runs a full turn: send, stream events, render reply and timeline", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("EventSource", MockEventSource);
    const fetchSpy = mockFetch({
      "GET api/me": () => jsonResponse(200, ME),
      "GET api/mock-phone/messages": () => jsonResponse(200, []),
      "POST api/chat": () => jsonResponse(202, { turnId: "t1" }),
    });
    render(<App />);

    expect(await screen.findByText("Alice Perera")).toBeInTheDocument();
    const scopes = screen.getByRole("group", { name: "Granted scopes" });
    expect(within(scopes).getByText("rooms:read")).toBeInTheDocument();

    const source = MockEventSource.latest();
    expect(source.url).toBe("api/events");
    act(() => {
      source.open();
    });
    expect(screen.getByText("Live")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Search rooms/ }));
    expect(fetchSpy).toHaveBeenCalledWith(
      "api/chat",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: JSON.stringify({
          message: "Find me a sea-view room in Galle for 14–16 November under $200",
        }),
      }),
    );
    expect(await screen.findByText(/sea-view room in Galle/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    act(() => {
      source.emit(ev({ type: "turn_started", turnId: "t1", message: "Find me a sea-view room" }));
      source.emit(
        ev({
          type: "tool_call_started",
          turnId: "t1",
          callId: "c1",
          tool: "search_rooms",
          server: "hotel-mcp",
          arguments: { city: "Galle" },
        }),
      );
    });
    expect(screen.getByText("Calling search_rooms…")).toBeInTheDocument();

    act(() => {
      source.emit(
        ev({
          type: "token_issued",
          turnId: "t1",
          callId: "c1",
          strategy: "auto-obo",
          scopes: ["rooms:read"],
          claims: claims({ act: { sub: "staywise-agent" } }),
        }),
      );
      source.emit(
        ev({
          type: "tool_call_finished",
          turnId: "t1",
          callId: "c1",
          ok: true,
          resultPreview: "2 rooms",
        }),
      );
      source.emit(
        ev({ type: "assistant_message", turnId: "t1", content: "Found **Lighthouse Deluxe**" }),
      );
      source.emit(ev({ type: "turn_finished", turnId: "t1" }));
    });

    expect(screen.getByText("Lighthouse Deluxe").tagName).toBe("STRONG");
    expect(screen.getByText("Used 1 tool")).toBeInTheDocument();
    const timeline = screen.getByRole("complementary", { name: "Security timeline" });
    expect(
      within(timeline).getByText("Auto step-up · delegated (token exchange)"),
    ).toBeInTheDocument();
    expect(within(timeline).getByText("alice → staywise-agent")).toBeInTheDocument();
    expect(screen.queryByText(/Calling|Thinking/)).not.toBeInTheDocument();

    // Granted scopes may change after a turn, so the session is refreshed.
    const updatedMe = { ...ME, grantedScopes: [...ME.grantedScopes, "payments:charge"] };
    fetchSpy.mockImplementation((input: RequestInfo | URL) =>
      Promise.resolve(input === "api/me" ? jsonResponse(200, updatedMe) : jsonResponse(404, {})),
    );
    act(() => {
      source.emit(ev({ type: "turn_finished", turnId: "t1" }));
    });
    await waitFor(() => {
      expect(within(scopes).getByText("payments:charge")).toBeInTheDocument();
    });
  });
});
