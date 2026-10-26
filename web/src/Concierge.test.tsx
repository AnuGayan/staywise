import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MeAuthenticated } from "./api/types";
import { Concierge } from "./Concierge";
import { MockEventSource, ev, jsonResponse, mfaTurn, noContent } from "./test/fixtures";
import { mockFetch, type Route } from "./test/fetchMock";

const ME: MeAuthenticated = {
  authenticated: true,
  user: { sub: "alice", name: "Alice Perera" },
  grantedScopes: ["openid"],
};

function createSource(url: string): EventSource {
  return new MockEventSource(url).asEventSource();
}

function setup(routes: Record<string, Route> = {}) {
  const fetchSpy = mockFetch({
    "GET api/mock-phone/messages": () => jsonResponse(200, []),
    ...routes,
  });
  const reload = vi.fn();
  const openPopup = vi.fn(() => ({ focus: vi.fn() }) as unknown as Window);
  render(
    <Concierge
      me={ME}
      onSessionStale={vi.fn()}
      createSource={createSource}
      openPopup={openPopup}
      reload={reload}
    />,
  );
  return { fetchSpy, reload, openPopup, source: MockEventSource.latest() };
}

afterEach(() => {
  MockEventSource.reset();
});

describe("Concierge", () => {
  it("sends with Enter, inserts a newline with Shift+Enter", async () => {
    const user = userEvent.setup();
    const { fetchSpy } = setup({ "POST api/chat": () => jsonResponse(202, { turnId: "t1" }) });
    const input = screen.getByLabelText("Message the concierge");
    await user.type(input, "Hello{Shift>}{Enter}{/Shift}there");
    expect(input).toHaveValue("Hello\nthere");
    expect(fetchSpy).not.toHaveBeenCalledWith("api/chat", expect.anything());

    await user.keyboard("{Enter}");
    expect(fetchSpy).toHaveBeenCalledWith(
      "api/chat",
      expect.objectContaining({ body: JSON.stringify({ message: "Hello\nthere" }) }),
    );
    expect(await screen.findByText(/Hello/)).toBeInTheDocument();
    expect(input).toHaveValue("");
  });

  it("shows the backend error when a turn is already running (409)", async () => {
    const user = userEvent.setup();
    setup({
      "POST api/chat": () => jsonResponse(409, { error: "A turn is already in progress" }),
    });
    await user.type(screen.getByLabelText("Message the concierge"), "Hi{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("A turn is already in progress");
    expect(screen.getByLabelText("Message the concierge")).toHaveValue("Hi");
  });

  it("disables sending while a turn runs and re-enables when it finishes", async () => {
    const user = userEvent.setup();
    const { source } = setup();
    act(() => {
      source.emit(ev({ type: "turn_started", turnId: "t1", message: "Book it" }));
    });
    await user.type(screen.getByLabelText("Message the concierge"), "next");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    act(() => {
      source.emit(ev({ type: "turn_finished", turnId: "t1" }));
    });
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it("drives the user step-up flow: dialog, popup, timeline states", async () => {
    const user = userEvent.setup();
    const { source, openPopup } = setup();
    const [started, toolStarted, required, resolved, ...rest] = mfaTurn();
    act(() => {
      [started, toolStarted, required].forEach((e) => {
        if (e) source.emit(e);
      });
    });
    const dialog = screen.getByRole("dialog", { name: "Extra verification needed" });
    const timeline = screen.getByRole("complementary", { name: "Security timeline" });
    expect(within(timeline).getByText("MFA requested — waiting for user")).toBeInTheDocument();
    expect(screen.getByText("Waiting for your verification…")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Verify with MFA" }));
    expect(openPopup).toHaveBeenCalledWith("/auth/stepup/s1");

    act(() => {
      if (resolved) source.emit(resolved);
      rest.forEach((e) => {
        source.emit(e);
      });
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(within(timeline).getByText("MFA approved")).toBeInTheDocument();
    expect(within(timeline).getByText("User step-up · MFA")).toBeInTheDocument();
  });

  it("declines a step-up through the cancel endpoint", async () => {
    const user = userEvent.setup();
    const { source, fetchSpy } = setup({ "POST api/stepup/s1/cancel": () => noContent() });
    act(() => {
      mfaTurn()
        .slice(0, 3)
        .forEach((e) => {
          source.emit(e);
        });
    });
    await user.click(screen.getByRole("button", { name: "Decline" }));
    expect(fetchSpy).toHaveBeenCalledWith(
      "api/stepup/s1/cancel",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
    act(() => {
      source.emit(
        ev({ type: "stepup_resolved", turnId: "t1", stepupId: "s1", outcome: "declined" }),
      );
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("User declined")).toBeInTheDocument();
  });

  it("resets the conversation and timeline", async () => {
    const user = userEvent.setup();
    const { source, fetchSpy } = setup({ "POST api/reset": () => noContent() });
    act(() => {
      mfaTurn().forEach((e) => {
        source.emit(e);
      });
    });
    expect(screen.getByText("Pay the deposit")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset" }));
    expect(fetchSpy).toHaveBeenCalledWith(
      "api/reset",
      expect.objectContaining({ method: "POST" }),
    );
    expect(await screen.findByText(/Hi! I.m your StayWise concierge/)).toBeInTheDocument();
    expect(screen.queryByText("Pay the deposit")).not.toBeInTheDocument();
    expect(screen.getByText("0 tool calls")).toBeInTheDocument();
  });

  it("signs out with POST /auth/logout then reloads", async () => {
    const user = userEvent.setup();
    const { fetchSpy, reload } = setup({ "POST auth/logout": () => noContent() });
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(fetchSpy).toHaveBeenCalledWith(
      "auth/logout",
      expect.objectContaining({ method: "POST" }),
    );
    await vi.waitFor(() => {
      expect(reload).toHaveBeenCalledTimes(1);
    });
  });

  it("shows phone messages with a large OTP and copies it", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { source } = setup({
      "GET api/mock-phone/messages": () =>
        jsonResponse(200, [
          { id: "m1", receivedAt: "2026-11-01T10:00:00Z", text: "Welcome to StayWise" },
        ]),
    });
    expect(await screen.findByText("Welcome to StayWise")).toBeInTheDocument();
    act(() => {
      source.emit(
        ev({
          type: "phone_message",
          id: "m2",
          receivedAt: "2026-11-01T10:05:00Z",
          text: "Your code is 482913",
          otp: "482913",
        }),
      );
    });
    expect(screen.getByText("482913")).toHaveClass("otp-code");
    expect(screen.getByText("Your code is 482913").closest("li")).toHaveClass("sms-live");
    await user.click(screen.getByRole("button", { name: "Copy code 482913" }));
    expect(writeText).toHaveBeenCalledWith("482913");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("collapses the phone and counts unread messages", async () => {
    const user = userEvent.setup();
    const { source } = setup();
    const toggle = screen.getByRole("button", { name: /Mock phone/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    act(() => {
      source.emit(
        ev({
          type: "phone_message",
          id: "m9",
          receivedAt: "2026-11-01T10:06:00Z",
          text: "Code 111222",
          otp: "111222",
        }),
      );
    });
    expect(screen.getByLabelText("1 new message")).toBeInTheDocument();
  });
});
