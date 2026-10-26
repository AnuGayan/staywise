import { useCallback, useEffect, useState } from "react";
import { fetchMe } from "../api/client";
import type { MeAuthenticated } from "../api/types";

export type SessionState =
  | { kind: "loading" }
  | { kind: "signedOut"; loginUrl: string }
  | { kind: "signedIn"; me: MeAuthenticated }
  | { kind: "error"; message: string };

export interface Session {
  session: SessionState;
  /** Re-fetches /api/me. When `silent`, keeps the current view while loading. */
  refresh: (silent?: boolean) => Promise<void>;
}

export function useSession(): Session {
  const [session, setSession] = useState<SessionState>({ kind: "loading" });

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setSession({ kind: "loading" });
    try {
      const me = await fetchMe();
      setSession(
        me.authenticated ? { kind: "signedIn", me } : { kind: "signedOut", loginUrl: me.loginUrl },
      );
    } catch (err: unknown) {
      if (silent) {
        // Keep the current view; the next refresh will try again.
        console.warn("StayWise: background session refresh failed", err);
        return;
      }
      setSession({
        kind: "error",
        message: err instanceof Error ? err.message : "Could not reach the StayWise server.",
      });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { session, refresh };
}
