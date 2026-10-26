import type { IssuedToken, StepUp, StepUpStatus, ToolCall } from "../state/types";
import {
  delegationLabel,
  formatAudience,
  formatClock,
  formatRemaining,
  splitScopes,
} from "../lib/format";
import { ScopeChips } from "./ScopeChips";
import { StrategyBadge } from "./StrategyBadge";

export interface TimelineCardProps {
  sequence: number;
  call: ToolCall;
  stepUps: StepUp[];
  requestedScopes: string[];
  now: number;
  /** The newest card shows its claims expanded; older ones collapse. */
  isLatest: boolean;
}

const STEPUP_LABEL: Record<StepUpStatus, string> = {
  pending: "MFA requested — waiting for user",
  approved: "MFA approved",
  declined: "User declined",
  timeout: "Step-up timed out",
};

const CALL_STATUS_LABEL: Record<ToolCall["status"], string> = {
  running: "Running",
  ok: "Succeeded",
  error: "Failed",
};

function StepUpRow({ stepUp, now }: { stepUp: StepUp; now: number }) {
  const remaining = Date.parse(stepUp.expiresAt) - now;
  return (
    <li className={`stepup-row stepup-${stepUp.status}`}>
      <span className="stepup-icon" aria-hidden="true" />
      <span className="stepup-text">
        <strong>{STEPUP_LABEL[stepUp.status]}</strong>
        {stepUp.status === "pending" && (
          <span className="muted"> · expires in {formatRemaining(remaining)}</span>
        )}
        {stepUp.reason.length > 0 && <span className="stepup-reason">{stepUp.reason}</span>}
      </span>
    </li>
  );
}

function Claims({ token, now }: { token: IssuedToken; now: number }) {
  const { claims } = token;
  const expiresIn = claims.exp * 1000 - now;
  const delegation = delegationLabel(claims.sub, claims.act);
  return (
    <dl className="claims">
      <dt>sub</dt>
      <dd>{claims.sub}</dd>
      <dt>client_id</dt>
      <dd>{claims.client_id}</dd>
      <dt>scope</dt>
      <dd>
        <ScopeChips scopes={splitScopes(claims.scope)} />
      </dd>
      <dt>aud</dt>
      <dd>{formatAudience(claims.aud)}</dd>
      <dt>exp</dt>
      <dd className={expiresIn <= 0 ? "claim-expired" : undefined}>
        {expiresIn <= 0 ? "expired" : `in ${formatRemaining(expiresIn)}`}
      </dd>
      <dt>act</dt>
      <dd>
        {delegation ? (
          <span className="delegation">{delegation}</span>
        ) : (
          <span className="muted">none (direct)</span>
        )}
      </dd>
      {claims.jti !== undefined && (
        <>
          <dt>jti</dt>
          <dd className="mono-trunc" title={claims.jti}>
            {claims.jti}
          </dd>
        </>
      )}
    </dl>
  );
}

export function TimelineCard({
  sequence,
  call,
  stepUps,
  requestedScopes,
  now,
  isLatest,
}: TimelineCardProps) {
  const strategyClass = call.token ? ` card-${call.token.strategy}` : "";
  return (
    <li className={`timeline-card card-status-${call.status}${strategyClass}`}>
      <div className="card-head">
        <span className="card-seq" aria-hidden="true">
          {sequence}
        </span>
        <div className="card-title">
          <span className="tool-name">{call.tool}</span>
          {call.server.length > 0 && <span className="server-name">on {call.server}</span>}
        </div>
        <span className={`status-pill status-${call.status}`}>
          {CALL_STATUS_LABEL[call.status]}
        </span>
      </div>

      <div className="card-strategy">
        {call.token ? (
          <StrategyBadge strategy={call.token.strategy} />
        ) : (
          <span className="badge badge-waiting">Obtaining token…</span>
        )}
        <span className="muted card-time">{formatClock(call.startedAt)}</span>
      </div>

      {stepUps.length > 0 && (
        <ul className="stepup-list" aria-label="Step-up status">
          {stepUps.map((s) => (
            <StepUpRow key={s.stepupId} stepUp={s} now={now} />
          ))}
        </ul>
      )}

      <div className="scope-grid">
        {requestedScopes.length > 0 && (
          <>
            <span className="scope-label">Required</span>
            <ScopeChips scopes={requestedScopes} />
          </>
        )}
        <span className="scope-label">Obtained</span>
        <ScopeChips
          scopes={call.token?.scopes ?? []}
          highlight={requestedScopes}
          emptyLabel="not yet"
        />
      </div>

      {call.token && (
        <details className="claims-details" open={isLatest}>
          <summary>Token claims</summary>
          <Claims token={call.token} now={now} />
        </details>
      )}

      {call.status === "error" && call.error !== undefined && (
        <p className="card-error" role="note">
          {call.error}
        </p>
      )}
    </li>
  );
}
