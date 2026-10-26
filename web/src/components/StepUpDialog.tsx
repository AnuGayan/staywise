import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { StepUp } from "../state/types";
import { useNow } from "../hooks/useNow";
import { formatRemaining } from "../lib/format";
import { ScopeChips } from "./ScopeChips";

export const STEPUP_WINDOW_NAME = "staywise-stepup";
export const STEPUP_WINDOW_FEATURES = "width=480,height=720";

export type OpenPopup = (url: string) => Window | null;

export interface StepUpDialogProps {
  stepUp: StepUp;
  onDecline: (stepupId: string) => Promise<void>;
  /** Injected for tests; defaults to window.open with the named popup. */
  openPopup?: OpenPopup;
}

type PopupState = "idle" | "opened" | "blocked";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

function defaultOpenPopup(url: string): Window | null {
  return window.open(url, STEPUP_WINDOW_NAME, STEPUP_WINDOW_FEATURES);
}

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
}

/** Keeps Tab / Shift+Tab inside `root`. */
function trapTab(event: KeyboardEvent, root: HTMLElement): void {
  const items = focusableWithin(root);
  const first = items[0];
  const last = items.at(-1);
  if (!first || !last) {
    event.preventDefault();
    return;
  }
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !root.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !root.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

export function StepUpDialog({
  stepUp,
  onDecline,
  openPopup = defaultOpenPopup,
}: StepUpDialogProps) {
  const titleId = useId();
  const descId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const [popup, setPopup] = useState<PopupState>("idle");
  const [declining, setDeclining] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const now = useNow(1000);
  const remaining = Date.parse(stepUp.expiresAt) - now;
  const expired = remaining <= 0;

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    primaryRef.current?.focus();
    return () => {
      previous?.focus();
    };
  }, []);

  const decline = useCallback(() => {
    if (declining) return;
    setDeclining(true);
    setError(null);
    onDecline(stepUp.stepupId).catch((err: unknown) => {
      setDeclining(false);
      setError(err instanceof Error ? err.message : "Could not decline the request.");
    });
  }, [declining, onDecline, stepUp.stepupId]);

  const verify = useCallback(() => {
    const win = openPopup(stepUp.startUrl);
    if (win) {
      win.focus();
      setPopup("opened");
    } else {
      setPopup("blocked");
    }
  }, [openPopup, stepUp.startUrl]);

  // Document-level so Esc and the Tab trap work wherever focus currently is.
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.preventDefault();
        decline();
        return;
      }
      const root = dialogRef.current;
      if (event.key === "Tab" && root) trapTab(event, root);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [decline]);

  return (
    <div className="modal-backdrop">
      <div
        ref={dialogRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
      >
        <div className="modal-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="28" height="28">
            <path
              d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3Zm0 6a3 3 0 0 1 1 5.83V16h-2v-2.17A3 3 0 0 1 12 8Z"
              fill="currentColor"
            />
          </svg>
        </div>
        <h2 id={titleId} className="modal-title">
          Extra verification needed
        </h2>
        <p id={descId} className="modal-lead">
          The concierge wants to use <strong className="tool-name">{stepUp.tool}</strong>, which
          needs you to confirm it&apos;s really you.
        </p>

        <dl className="modal-facts">
          <dt>Why</dt>
          <dd>
            {stepUp.reason.length > 0
              ? stepUp.reason
              : "This action requires a higher assurance level."}
          </dd>
          <dt>Scopes requested</dt>
          <dd>
            <ScopeChips scopes={stepUp.scope} highlight={stepUp.scope} />
          </dd>
          <dt>Expires</dt>
          <dd>
            <span className={expired ? "countdown countdown-expired" : "countdown"} role="timer">
              {expired ? "Expired" : formatRemaining(remaining)}
            </span>
          </dd>
        </dl>

        {popup === "opened" && (
          <p className="modal-note" role="status">
            Complete the sign-in in the popup window. This dialog closes automatically when
            you&apos;re done.
          </p>
        )}
        {popup === "blocked" && (
          <p className="modal-note modal-warn" role="alert">
            Your browser blocked the popup.{" "}
            <a href={stepUp.startUrl} target={STEPUP_WINDOW_NAME}>
              Open the verification page
            </a>{" "}
            instead.
          </p>
        )}
        {error !== null && (
          <p className="modal-note modal-error" role="alert">
            {error}
          </p>
        )}

        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={decline} disabled={declining}>
            {declining ? "Declining…" : "Decline"}
          </button>
          <button
            ref={primaryRef}
            type="button"
            className="btn btn-primary"
            onClick={verify}
            disabled={expired || declining}
          >
            {popup === "opened" ? "Reopen MFA window" : "Verify with MFA"}
          </button>
        </div>
      </div>
    </div>
  );
}
