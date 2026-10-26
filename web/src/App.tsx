import { useCallback } from "react";
import { useSession } from "./hooks/useSession";
import { Concierge } from "./Concierge";
import { SignIn } from "./components/SignIn";

export function App() {
  const { session, refresh } = useSession();

  const refreshSilently = useCallback(() => {
    void refresh(true);
  }, [refresh]);

  const retry = useCallback(() => {
    void refresh();
  }, [refresh]);

  switch (session.kind) {
    case "loading":
      return (
        <div className="center-screen" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          Loading StayWise…
        </div>
      );
    case "signedOut":
      return <SignIn loginUrl={session.loginUrl} />;
    case "error":
      return (
        <div className="center-screen" role="alert">
          <p>{session.message}</p>
          <button type="button" className="btn btn-primary" onClick={retry}>
            Try again
          </button>
        </div>
      );
    case "signedIn":
      return <Concierge me={session.me} onSessionStale={refreshSilently} />;
  }
}
