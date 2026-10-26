import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useReducer } from "react";
import { describe, expect, it, vi } from "vitest";
import type { ServerEvent } from "../api/types";
import { conciergeReducer, initialState } from "../state/reducer";
import type { ConciergeAction, StepUp } from "../state/types";
import { ev, mfaTurn } from "../test/fixtures";
import { STEPUP_WINDOW_FEATURES, STEPUP_WINDOW_NAME, StepUpDialog } from "./StepUpDialog";
import { StepUpLayer } from "./StepUpLayer";

function stepUp(overrides: Partial<StepUp> = {}): StepUp {
  return {
    stepupId: "s1",
    callId: "c1",
    turnId: "t1",
    tool: "charge_card",
    scope: ["payments:charge"],
    reason: "Charging a saved card requires MFA",
    startUrl: "/auth/stepup/s1",
    expiresAt: new Date(Date.now() + 125_000).toISOString(),
    requestedAt: new Date().toISOString(),
    status: "pending",
    ...overrides,
  };
}

const fakeWindow = { focus: vi.fn() } as unknown as Window;

describe("StepUpDialog", () => {
  it("renders an accessible modal with tool, reason, scopes and countdown", () => {
    render(<StepUpDialog stepUp={stepUp()} onDecline={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Extra verification needed" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveTextContent("charge_card");
    expect(dialog).toHaveTextContent("Charging a saved card requires MFA");
    expect(dialog).toHaveTextContent("payments:charge");
    expect(screen.getByRole("timer")).toHaveTextContent(/^2m 0[45]s$/);
    expect(screen.getByRole("button", { name: "Verify with MFA" })).toHaveFocus();
  });

  it("opens the named step-up popup on Verify", async () => {
    const user = userEvent.setup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(fakeWindow);
    render(<StepUpDialog stepUp={stepUp()} onDecline={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Verify with MFA" }));
    expect(openSpy).toHaveBeenCalledWith(
      "/auth/stepup/s1",
      STEPUP_WINDOW_NAME,
      STEPUP_WINDOW_FEATURES,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/popup window/);
    expect(screen.getByRole("button", { name: "Reopen MFA window" })).toBeInTheDocument();
  });

  it("shows a fallback link when the popup is blocked", async () => {
    const user = userEvent.setup();
    render(<StepUpDialog stepUp={stepUp()} onDecline={vi.fn()} openPopup={() => null} />);
    await user.click(screen.getByRole("button", { name: "Verify with MFA" }));
    const link = screen.getByRole("link", { name: "Open the verification page" });
    expect(link).toHaveAttribute("href", "/auth/stepup/s1");
    expect(link).toHaveAttribute("target", STEPUP_WINDOW_NAME);
  });

  it("calls cancel with the step-up id when Decline is clicked", async () => {
    const user = userEvent.setup();
    const onDecline = vi.fn<(id: string) => Promise<void>>().mockResolvedValue(undefined);
    render(<StepUpDialog stepUp={stepUp()} onDecline={onDecline} />);
    await user.click(screen.getByRole("button", { name: "Decline" }));
    expect(onDecline).toHaveBeenCalledWith("s1");
    expect(screen.getByRole("button", { name: "Declining…" })).toBeDisabled();
  });

  it("treats Escape as Decline", async () => {
    const user = userEvent.setup();
    const onDecline = vi.fn<(id: string) => Promise<void>>().mockResolvedValue(undefined);
    render(<StepUpDialog stepUp={stepUp()} onDecline={onDecline} />);
    await user.keyboard("{Escape}");
    expect(onDecline).toHaveBeenCalledWith("s1");
  });

  it("shows an error and re-enables Decline if cancel fails", async () => {
    const user = userEvent.setup();
    const onDecline = vi
      .fn<(id: string) => Promise<void>>()
      .mockRejectedValue(new Error("Network down"));
    render(<StepUpDialog stepUp={stepUp()} onDecline={onDecline} />);
    await user.click(screen.getByRole("button", { name: "Decline" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Network down");
    expect(screen.getByRole("button", { name: "Decline" })).toBeEnabled();
  });

  it("traps focus inside the dialog", async () => {
    const user = userEvent.setup();
    render(
      <>
        <button type="button">outside</button>
        <StepUpDialog stepUp={stepUp()} onDecline={vi.fn()} />
      </>,
    );
    const verify = screen.getByRole("button", { name: "Verify with MFA" });
    const decline = screen.getByRole("button", { name: "Decline" });
    expect(verify).toHaveFocus();
    await user.tab();
    expect(decline).toHaveFocus();
    await user.tab({ shift: true });
    expect(verify).toHaveFocus();
    await user.tab({ shift: true });
    expect(decline).toHaveFocus();
  });

  it("disables Verify once the request has expired", () => {
    render(
      <StepUpDialog
        stepUp={stepUp({ expiresAt: new Date(Date.now() - 1000).toISOString() })}
        onDecline={vi.fn()}
      />,
    );
    expect(screen.getByRole("timer")).toHaveTextContent("Expired");
    expect(screen.getByRole("button", { name: "Verify with MFA" })).toBeDisabled();
  });
});

describe("StepUpLayer", () => {
  let push: (action: ConciergeAction) => void = () => undefined;

  function Harness({ onDecline }: { onDecline: (id: string) => Promise<void> }) {
    const [state, dispatch] = useReducer(conciergeReducer, initialState);
    push = dispatch;
    return <StepUpLayer state={state} onDecline={onDecline} openPopup={() => fakeWindow} />;
  }

  const events = mfaTurn();
  const required = events.filter((e) => e.type !== "stepup_resolved").slice(0, 3);

  function send(list: ServerEvent[]): void {
    act(() => {
      list.forEach(push);
    });
  }

  it("opens on stepup_required and closes on the matching stepup_resolved", async () => {
    render(<Harness onDecline={vi.fn()} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    send(required);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    send([ev({ type: "stepup_resolved", turnId: "t1", stepupId: "other", outcome: "approved" })]);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    send([ev({ type: "stepup_resolved", turnId: "t1", stepupId: "s1", outcome: "approved" })]);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("stays open after Decline until the backend confirms with stepup_resolved", async () => {
    const user = userEvent.setup();
    const onDecline = vi.fn<(id: string) => Promise<void>>().mockResolvedValue(undefined);
    render(<Harness onDecline={onDecline} />);
    send(required);
    await user.click(screen.getByRole("button", { name: "Decline" }));
    expect(onDecline).toHaveBeenCalledWith("s1");
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    send([ev({ type: "stepup_resolved", turnId: "t1", stepupId: "s1", outcome: "declined" })]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
