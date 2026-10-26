import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import type { ConciergeState, Turn } from "../state/types";
import { selectCallsForTurn, selectStepUpsForCall, selectTurns } from "../state/selectors";
import { SUGGESTED_PROMPTS, type SuggestedPrompt } from "../lib/prompts";
import { MessageBubble, ToolCallList } from "./MessageBubble";

export interface ChatPaneProps {
  state: ConciergeState;
  busy: boolean;
  sendError: string | null;
  /** Resolves true when the backend accepted the message. */
  onSend: (message: string) => Promise<boolean>;
}

function activityText(state: ConciergeState, turn: Turn): string {
  const calls = selectCallsForTurn(state, turn);
  const current = calls.filter((c) => c.status === "running").at(-1);
  if (!current) return calls.length > 0 ? "Putting it together…" : "Thinking…";
  const waitingForUser = selectStepUpsForCall(state, current).some((s) => s.status === "pending");
  if (waitingForUser) return "Waiting for your verification…";
  return `Calling ${current.tool}…`;
}

function TypingIndicator({ text }: { text: string }) {
  return (
    <li className="msg msg-assistant">
      <div className="bubble bubble-typing" role="status" aria-live="polite">
        <span className="dots" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
        <span className="typing-text">{text}</span>
      </div>
    </li>
  );
}

function TurnView({ state, turn }: { state: ConciergeState; turn: Turn }) {
  const calls = selectCallsForTurn(state, turn);
  const lastReplyId = turn.replies.at(-1)?.id;
  return (
    <>
      {turn.userMessage.length > 0 && <MessageBubble variant="user" content={turn.userMessage} />}
      {turn.replies.map((reply) => (
        <MessageBubble key={reply.id} variant="assistant" content={reply.content}>
          {reply.id === lastReplyId && turn.status === "finished" && <ToolCallList calls={calls} />}
        </MessageBubble>
      ))}
      {turn.status === "failed" && (
        <MessageBubble
          variant="error"
          content={`Sorry — something went wrong. ${turn.error ?? ""}`.trim()}
        >
          <ToolCallList calls={calls} />
        </MessageBubble>
      )}
      {turn.status === "finished" && !lastReplyId && calls.length > 0 && (
        <MessageBubble variant="assistant" content="*Done — no reply text.*">
          <ToolCallList calls={calls} />
        </MessageBubble>
      )}
      {turn.status === "running" && <TypingIndicator text={activityText(state, turn)} />}
    </>
  );
}

function PromptChip({
  prompt,
  disabled,
  onPick,
  compact,
}: {
  prompt: SuggestedPrompt;
  disabled: boolean;
  onPick: (text: string) => void;
  compact: boolean;
}) {
  const handleClick = useCallback(() => {
    onPick(prompt.text);
  }, [onPick, prompt.text]);
  return (
    <li>
      <button
        type="button"
        className="prompt-chip"
        disabled={disabled}
        onClick={handleClick}
        title={prompt.text}
      >
        <span className="prompt-step" aria-hidden="true">
          {prompt.step}
        </span>
        <span className="prompt-body">
          <span className="prompt-title">{prompt.title}</span>
          {!compact && <span className="prompt-text">{prompt.text}</span>}
        </span>
      </button>
    </li>
  );
}

function PromptChips({
  prompts,
  disabled,
  onPick,
  compact,
}: {
  prompts: readonly SuggestedPrompt[];
  disabled: boolean;
  onPick: (text: string) => void;
  compact: boolean;
}) {
  return (
    <ul className={compact ? "prompts prompts-compact" : "prompts"} aria-label="Suggested prompts">
      {prompts.map((p) => (
        <PromptChip key={p.id} prompt={p} disabled={disabled} onPick={onPick} compact={compact} />
      ))}
    </ul>
  );
}

export function ChatPane({ state, busy, sendError, onSend }: ChatPaneProps) {
  const [draft, setDraft] = useState("");
  const [showScript, setShowScript] = useState(false);
  const inputId = useId();
  const scrollRef = useRef<HTMLDivElement>(null);
  const turns = selectTurns(state);
  const isEmpty = turns.length === 0;
  const canSend = !busy && draft.trim().length > 0;

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [state]);

  const submit = useCallback(
    (text: string) => {
      const message = text.trim();
      if (busy || message.length === 0) return;
      void onSend(message).then((accepted) => {
        if (accepted) setDraft((current) => (current.trim() === message ? "" : current));
      });
    },
    [busy, onSend],
  );

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      submit(draft);
    },
    [draft, submit],
  );

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
      event.preventDefault();
      submit(draft);
    },
    [draft, submit],
  );

  const handleChange = useCallback((event: ChangeEvent<HTMLTextAreaElement>) => {
    setDraft(event.target.value);
  }, []);

  const toggleScript = useCallback(() => {
    setShowScript((v) => !v);
  }, []);

  return (
    <main className="chat" aria-labelledby="chat-title">
      <h2 id="chat-title" className="visually-hidden">
        Conversation
      </h2>
      <div className="chat-scroll" ref={scrollRef}>
        {isEmpty ? (
          <div className="chat-empty">
            <h3>Hi! I&apos;m your StayWise concierge.</h3>
            <p className="muted">
              Try the demo script below — watch the security timeline as the agent steps up its
              access.
            </p>
            <PromptChips
              prompts={SUGGESTED_PROMPTS}
              disabled={busy}
              onPick={submit}
              compact={false}
            />
          </div>
        ) : (
          <ol className="msg-list" aria-label="Messages">
            {turns.map((turn) => (
              <TurnView key={turn.turnId} state={state} turn={turn} />
            ))}
          </ol>
        )}
      </div>

      <div className="composer-wrap">
        {!isEmpty && (
          <div className="script-row">
            <button
              type="button"
              className="link-btn"
              aria-expanded={showScript}
              onClick={toggleScript}
            >
              {showScript ? "Hide demo script" : "Show demo script"}
            </button>
            {showScript && (
              <PromptChips prompts={SUGGESTED_PROMPTS} disabled={busy} onPick={submit} compact />
            )}
          </div>
        )}
        {sendError !== null && (
          <p className="send-error" role="alert">
            {sendError}
          </p>
        )}
        <form className="composer" onSubmit={handleSubmit}>
          <label htmlFor={inputId} className="visually-hidden">
            Message the concierge
          </label>
          <textarea
            id={inputId}
            className="composer-input"
            rows={1}
            placeholder={
              busy ? "The concierge is working…" : "Ask about rooms, bookings, payments…"
            }
            value={draft}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
          />
          <button type="submit" className="btn btn-primary" disabled={!canSend}>
            Send
          </button>
        </form>
        <p className="composer-hint muted">Enter to send · Shift+Enter for a new line</p>
      </div>
    </main>
  );
}
