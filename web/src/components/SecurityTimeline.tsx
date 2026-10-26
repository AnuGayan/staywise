import { useEffect, useRef } from "react";
import type { ConciergeState } from "../state/types";
import { selectRequestedScopes, selectStepUpsForCall, selectTimeline } from "../state/selectors";
import { useNow } from "../hooks/useNow";
import { STRATEGY_INFO } from "../lib/format";
import { StrategyBadge } from "./StrategyBadge";
import { TimelineCard } from "./TimelineCard";
import type { TokenStrategy } from "../api/types";

const LEGEND_ORDER: readonly TokenStrategy[] = ["cached", "auto-obo", "auto-agent", "user-mfa"];

export function SecurityTimeline({ state }: { state: ConciergeState }) {
  const calls = selectTimeline(state);
  const now = useNow(1000, calls.length > 0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const latest = calls.at(-1);
  const latestId = latest?.callId;

  useEffect(() => {
    const el = scrollRef.current;
    if (latest === undefined || !el) return;
    el.scrollTop = el.scrollHeight;
    // Re-run whenever the newest card changes (new token, step-up, result) so it stays visible.
  }, [latest]);

  return (
    <aside className="timeline" aria-labelledby="timeline-title">
      <div className="panel-head">
        <h2 id="timeline-title">Security timeline</h2>
        <span className="muted">
          {calls.length} tool call{calls.length === 1 ? "" : "s"}
        </span>
      </div>

      <div className="panel-scroll" ref={scrollRef}>
        {calls.length === 0 ? (
          <div className="timeline-empty">
            <p>Every tool the agent calls appears here with the token it used and how it got it.</p>
            <ul className="legend" aria-label="Token strategies">
              {LEGEND_ORDER.map((strategy) => (
                <li key={strategy}>
                  <StrategyBadge strategy={strategy} />
                  <span className="legend-desc">{STRATEGY_INFO[strategy].description}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <ol className="timeline-list" aria-live="polite">
            {calls.map((call, position) => (
              <TimelineCard
                key={call.callId}
                sequence={position + 1}
                call={call}
                stepUps={selectStepUpsForCall(state, call)}
                requestedScopes={selectRequestedScopes(state, call)}
                now={now}
                isLatest={call.callId === latestId}
              />
            ))}
          </ol>
        )}
      </div>
    </aside>
  );
}
