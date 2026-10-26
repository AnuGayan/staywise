import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { PhoneEntry } from "../state/types";
import { useNow } from "../hooks/useNow";
import { formatClock } from "../lib/format";

export interface MockPhoneProps {
  messages: PhoneEntry[];
  loadError: string | null;
  defaultOpen?: boolean;
}

type CopyState = "idle" | "copied" | "failed";

function OtpCode({ otp }: { otp: string }) {
  const [copy, setCopy] = useState<CopyState>("idle");

  useEffect(() => {
    if (copy === "idle") return undefined;
    const id = window.setTimeout(() => {
      setCopy("idle");
    }, 1800);
    return () => {
      window.clearTimeout(id);
    };
  }, [copy]);

  const handleCopy = useCallback(() => {
    const markFailed = (): void => {
      setCopy("failed");
    };
    try {
      // Clipboard is unavailable on insecure origins; writeText then throws synchronously.
      navigator.clipboard.writeText(otp).then(() => {
        setCopy("copied");
      }, markFailed);
    } catch {
      markFailed();
    }
  }, [otp]);

  const label = copy === "copied" ? "Copied" : copy === "failed" ? "Copy failed" : "Copy";
  return (
    <div className="otp">
      <span className="otp-code" aria-label={`One-time code ${otp.split("").join(" ")}`}>
        {otp}
      </span>
      <button
        type="button"
        className="btn btn-small"
        onClick={handleCopy}
        aria-label={`Copy code ${otp}`}
      >
        {label}
      </button>
    </div>
  );
}

function SmsBubble({ message }: { message: PhoneEntry }) {
  return (
    <li className={message.live ? "sms sms-live" : "sms"}>
      <p className="sms-text">{message.text}</p>
      {message.otp !== undefined && message.otp.length > 0 && <OtpCode otp={message.otp} />}
      <time className="sms-time" dateTime={message.receivedAt}>
        {formatClock(message.receivedAt)}
      </time>
    </li>
  );
}

export function MockPhone({ messages, loadError, defaultOpen = true }: MockPhoneProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [seenCount, setSeenCount] = useState(messages.length);
  const bodyId = useId();
  const scrollRef = useRef<HTMLDivElement>(null);
  const now = useNow(30_000, open);
  const unread = open ? 0 : Math.max(0, messages.length - seenCount);
  const latestId = messages.at(-1)?.id;

  useEffect(() => {
    const el = scrollRef.current;
    if (!open || !el || latestId === undefined) return;
    el.scrollTop = el.scrollHeight;
  }, [latestId, open]);

  const toggle = useCallback(() => {
    setOpen((wasOpen) => !wasOpen);
    setSeenCount(messages.length);
  }, [messages.length]);

  return (
    <section
      className={open ? "phone-panel" : "phone-panel phone-collapsed"}
      aria-label="Mock phone"
    >
      <button
        type="button"
        className="phone-toggle"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={toggle}
      >
        <span className="phone-toggle-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <path
              d="M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm0 3v13h10V5H7Zm4 14v1h2v-1h-2Z"
              fill="currentColor"
            />
          </svg>
        </span>
        <span className="phone-toggle-label">Mock phone</span>
        {unread > 0 && (
          <span className="unread" aria-label={`${unread} new message${unread === 1 ? "" : "s"}`}>
            {unread}
          </span>
        )}
      </button>

      <div id={bodyId} className="phone" hidden={!open}>
        <div className="phone-notch" aria-hidden="true" />
        <div className="phone-status" aria-hidden="true">
          <span>
            {new Date(now).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </span>
          <span>5G ▮▮▮</span>
        </div>
        <div className="phone-app-bar">
          <span className="phone-avatar" aria-hidden="true">
            ID
          </span>
          <div>
            <strong>StayWise ID</strong>
            <span className="muted">SMS · verification</span>
          </div>
        </div>
        <div className="phone-screen" ref={scrollRef}>
          {loadError !== null && (
            <p className="phone-empty" role="alert">
              {loadError}
            </p>
          )}
          {messages.length === 0 && loadError === null ? (
            <p className="phone-empty">No messages yet. OTP codes sent during MFA appear here.</p>
          ) : (
            <ol className="sms-list" aria-live="polite" aria-label="Text messages">
              {messages.map((m) => (
                <SmsBubble key={m.id} message={m} />
              ))}
            </ol>
          )}
        </div>
        <div className="phone-home" aria-hidden="true" />
      </div>
    </section>
  );
}
