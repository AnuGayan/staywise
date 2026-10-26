import type { ReactNode } from "react";
import type { ToolCall } from "../state/types";
import { Markdown } from "../lib/markdown";
import { formatArguments } from "../lib/format";

export type BubbleVariant = "user" | "assistant" | "error";

export interface MessageBubbleProps {
  variant: BubbleVariant;
  content: string;
  children?: ReactNode;
}

const AUTHOR: Record<BubbleVariant, string> = {
  user: "You",
  assistant: "Concierge",
  error: "Concierge",
};

export function MessageBubble({ variant, content, children }: MessageBubbleProps) {
  return (
    <li className={`msg msg-${variant}`}>
      <span className="visually-hidden">{AUTHOR[variant]}:</span>
      <div className="bubble">
        {variant === "user" ? (
          <p className="bubble-plain">{content}</p>
        ) : (
          <Markdown text={content} />
        )}
        {children}
      </div>
    </li>
  );
}

const STATUS_TEXT: Record<ToolCall["status"], string> = {
  running: "running",
  ok: "ok",
  error: "error",
};

export function ToolCallList({ calls }: { calls: ToolCall[] }) {
  if (calls.length === 0) return null;
  return (
    <details className="toolcalls">
      <summary>
        Used {calls.length} tool{calls.length === 1 ? "" : "s"}
        <span className="toolcalls-names">· {calls.map((c) => c.tool).join(", ")}</span>
      </summary>
      <ul className="toolcall-list">
        {calls.map((call) => (
          <li key={call.callId} className={`toolcall toolcall-${call.status}`}>
            <div className="toolcall-head">
              <code className="tool-name">{call.tool}</code>
              <span className={`status-pill status-${call.status}`}>
                {STATUS_TEXT[call.status]}
              </span>
            </div>
            <span className="toolcall-label">Arguments</span>
            <pre className="toolcall-pre">{formatArguments(call.arguments)}</pre>
            {call.resultPreview !== undefined && call.resultPreview.length > 0 && (
              <>
                <span className="toolcall-label">Result</span>
                <pre className="toolcall-pre">{call.resultPreview}</pre>
              </>
            )}
            {call.error !== undefined && (
              <>
                <span className="toolcall-label">Error</span>
                <pre className="toolcall-pre toolcall-error">{call.error}</pre>
              </>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
