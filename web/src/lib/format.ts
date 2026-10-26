import type { ActClaim, TokenStrategy } from "../api/types";

export interface StrategyInfo {
  label: string;
  description: string;
}

export const STRATEGY_INFO: Record<TokenStrategy, StrategyInfo> = {
  cached: {
    label: "Existing token",
    description: "Reused a token the agent already held with enough scope.",
  },
  "auto-obo": {
    label: "Auto step-up · delegated (token exchange)",
    description: "Agent exchanged the user's token for a narrower one acting on the user's behalf.",
  },
  "auto-agent": {
    label: "Auto step-up · agent identity",
    description: "Agent obtained a token with its own identity; no user involvement needed.",
  },
  "user-mfa": {
    label: "User step-up · MFA",
    description: "User re-authenticated with MFA to grant a higher-risk scope.",
  },
};

/** Flattens a nested RFC 8693 `act` claim into actor subjects, outermost first. */
export function actorChain(act: ActClaim | undefined): string[] {
  const chain: string[] = [];
  let current = act;
  while (current) {
    chain.push(current.sub);
    current = current.act;
  }
  return chain;
}

/** "user → acting agent" (→ prior actors for multi-hop delegation). */
export function delegationLabel(sub: string, act: ActClaim | undefined): string | null {
  const chain = actorChain(act);
  if (chain.length === 0) return null;
  return [sub, ...chain].join(" → ");
}

/** Formats a remaining duration such as "4m 05s", "59s", or "expired". */
export function formatRemaining(ms: number): string {
  if (ms <= 0) return "expired";
  const totalSeconds = Math.ceil(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatAudience(aud: string | string[]): string {
  return Array.isArray(aud) ? aud.join(", ") : aud;
}

export function splitScopes(scope: string): string[] {
  return scope.split(/\s+/).filter((s) => s.length > 0);
}

export function formatArguments(args: Record<string, unknown>): string {
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return "[unserializable arguments]";
  }
}
