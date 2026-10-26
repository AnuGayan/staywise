import { useCallback, useEffect, useReducer, useState } from "react";
import {
  cancelStepUp,
  fetchPhoneMessages,
  logout,
  resetConversation,
  sendChat,
} from "./api/client";
import type { MeAuthenticated, ServerEvent } from "./api/types";
import { useEventStream, type EventStreamOptions } from "./hooks/useEventStream";
import { conciergeReducer, initialState } from "./state/reducer";
import { selectIsTurnRunning } from "./state/selectors";
import { ChatPane } from "./components/ChatPane";
import { Header } from "./components/Header";
import { MockPhone } from "./components/MockPhone";
import { SecurityTimeline } from "./components/SecurityTimeline";
import { StepUpLayer } from "./components/StepUpLayer";
import type { OpenPopup } from "./components/StepUpDialog";

export interface ConciergeProps {
  me: MeAuthenticated;
  /** Called when granted scopes may have changed (after a turn ends). */
  onSessionStale: () => void;
  createSource?: EventStreamOptions["createSource"];
  openPopup?: OpenPopup;
  reload?: () => void;
}

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function defaultReload(): void {
  window.location.reload();
}

export function Concierge({
  me,
  onSessionStale,
  createSource,
  openPopup,
  reload = defaultReload,
}: ConciergeProps) {
  const [state, dispatch] = useReducer(conciergeReducer, initialState);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [phoneError, setPhoneError] = useState<string | null>(null);

  const handleEvent = useCallback(
    (event: ServerEvent) => {
      dispatch(event);
      if (event.type === "turn_finished" || event.type === "turn_failed") onSessionStale();
    },
    [onSessionStale],
  );

  const connection = useEventStream(
    "api/events",
    handleEvent,
    createSource ? { createSource } : {},
  );

  useEffect(() => {
    let cancelled = false;
    fetchPhoneMessages().then(
      (messages) => {
        if (!cancelled) dispatch({ type: "local/phone_loaded", messages });
      },
      (err: unknown) => {
        if (!cancelled) setPhoneError(messageOf(err, "Could not load messages."));
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSend = useCallback(async (message: string): Promise<boolean> => {
    setSending(true);
    setSendError(null);
    try {
      const result = await sendChat(message);
      if (!result.ok) {
        setSendError(result.error);
        return false;
      }
      dispatch({
        type: "local/turn_submitted",
        turnId: result.turnId,
        message,
        ts: new Date().toISOString(),
      });
      return true;
    } catch (err: unknown) {
      setSendError(messageOf(err, "Message could not be sent."));
      return false;
    } finally {
      setSending(false);
    }
  }, []);

  const handleReset = useCallback(() => {
    setResetting(true);
    setBanner(null);
    resetConversation()
      .then(() => {
        dispatch({ type: "local/reset" });
        setSendError(null);
      })
      .catch((err: unknown) => {
        setBanner(messageOf(err, "Reset failed."));
      })
      .finally(() => {
        setResetting(false);
      });
  }, []);

  const handleSignOut = useCallback(() => {
    setBanner(null);
    logout().then(reload, (err: unknown) => {
      setBanner(messageOf(err, "Sign out failed."));
    });
  }, [reload]);

  const dismissBanner = useCallback(() => {
    setBanner(null);
  }, []);

  const busy = sending || selectIsTurnRunning(state);

  return (
    <div className="app">
      <Header
        user={me.user}
        grantedScopes={me.grantedScopes}
        connection={connection}
        resetting={resetting}
        onReset={handleReset}
        onSignOut={handleSignOut}
      />
      {banner !== null && (
        <div className="banner" role="alert">
          <span>{banner}</span>
          <button type="button" className="link-btn" onClick={dismissBanner}>
            Dismiss
          </button>
        </div>
      )}
      <div className="layout">
        <MockPhone messages={state.phone} loadError={phoneError} />
        <ChatPane state={state} busy={busy} sendError={sendError} onSend={handleSend} />
        <SecurityTimeline state={state} />
      </div>
      <StepUpLayer state={state} onDecline={cancelStepUp} {...(openPopup ? { openPopup } : {})} />
    </div>
  );
}
