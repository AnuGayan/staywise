import { useEffect, useRef, useState } from "react";
import { parseServerEvent } from "../api/events";
import { SERVER_EVENT_TYPES, type ServerEvent } from "../api/types";

export type ConnectionStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface EventStreamOptions {
  /** When false, no connection is opened (e.g. before sign-in). */
  enabled?: boolean;
  /** Factory for tests; defaults to the browser EventSource. */
  createSource?: (url: string) => EventSource;
}

/** EventSource.CLOSED; inlined so the hook does not depend on the global at runtime. */
const READY_STATE_CLOSED = 2;

function defaultCreateSource(url: string): EventSource {
  // Same-origin, so the session cookie is sent without withCredentials.
  return new EventSource(url);
}

/**
 * Subscribes to the backend SSE stream for the lifetime of the component.
 * One EventSource per mount; the browser reconnects it automatically.
 * `onEvent` may change between renders without reopening the stream.
 */
export function useEventStream(
  url: string,
  onEvent: (event: ServerEvent) => void,
  { enabled = true, createSource = defaultCreateSource }: EventStreamOptions = {},
): ConnectionStatus {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const handlerRef = useRef(onEvent);
  const factoryRef = useRef(createSource);

  useEffect(() => {
    handlerRef.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    if (!enabled) return undefined;
    const source = factoryRef.current(url);
    setStatus("connecting");

    const handleOpen = (): void => {
      setStatus("open");
    };
    const handleError = (): void => {
      setStatus(source.readyState === READY_STATE_CLOSED ? "closed" : "reconnecting");
    };
    const handleMessage = (message: MessageEvent<string>): void => {
      const event = parseServerEvent(message.type, message.data);
      if (event) handlerRef.current(event);
    };

    source.addEventListener("open", handleOpen);
    source.addEventListener("error", handleError);
    for (const type of SERVER_EVENT_TYPES) {
      source.addEventListener(type, handleMessage);
    }

    return () => {
      source.removeEventListener("open", handleOpen);
      source.removeEventListener("error", handleError);
      for (const type of SERVER_EVENT_TYPES) {
        source.removeEventListener(type, handleMessage);
      }
      source.close();
    };
  }, [url, enabled]);

  return status;
}
