import type { User } from "../api/types";
import type { ConnectionStatus } from "../hooks/useEventStream";

export interface HeaderProps {
  user: User;
  grantedScopes: string[];
  connection: ConnectionStatus;
  resetting: boolean;
  onReset: () => void;
  onSignOut: () => void;
}

const CONNECTION_LABEL: Record<ConnectionStatus, string> = {
  connecting: "Connecting…",
  open: "Live",
  reconnecting: "Reconnecting…",
  closed: "Disconnected",
};

function initials(name: string): string {
  const parts = name
    .trim()
    .split(/\s+/)
    .filter((p) => p.length > 0);
  const letters = parts.slice(0, 2).map((p) => p.charAt(0).toUpperCase());
  return letters.join("") || "?";
}

export function Header({
  user,
  grantedScopes,
  connection,
  resetting,
  onReset,
  onSignOut,
}: HeaderProps) {
  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          SW
        </span>
        <div>
          <h1 className="brand-title">StayWise Concierge</h1>
          <p className="brand-sub">AI concierge · OAuth step-up demo</p>
        </div>
      </div>

      <div className="header-scopes" role="group" aria-label="Granted scopes">
        <span className="header-label">Session scopes</span>
        <ul className="chip-list">
          {grantedScopes.length === 0 ? (
            <li className="chip chip-muted">none</li>
          ) : (
            grantedScopes.map((scope) => (
              <li key={scope} className="chip">
                {scope}
              </li>
            ))
          )}
        </ul>
      </div>

      <div className="header-actions">
        <span
          className={`conn conn-${connection}`}
          role="status"
          aria-live="polite"
          title="Live event stream from the agent"
        >
          <span className="conn-dot" aria-hidden="true" />
          {CONNECTION_LABEL[connection]}
        </span>
        <span className="user" title={user.sub}>
          <span className="avatar" aria-hidden="true">
            {initials(user.name)}
          </span>
          <span className="user-name">{user.name}</span>
        </span>
        <button type="button" className="btn btn-ghost" onClick={onReset} disabled={resetting}>
          {resetting ? "Resetting…" : "Reset"}
        </button>
        <button type="button" className="btn btn-ghost" onClick={onSignOut}>
          Sign out
        </button>
      </div>
    </header>
  );
}
