import * as React from "react";
import { openSse } from "@/lib/sse";
import { endpoints } from "@/lib/api";
import { useDebate } from "@/store/debate";
import type { DebateEvent } from "@/types/v4";

/**
 * Open the SSE stream for a debate session and feed events into the
 * shared store. Returns a `disconnect` callback that the caller can
 * invoke to stop early.
 */
export function useDebateStream(sessionId: string | null, autoStart = true) {
  const apply = useDebate((s) => s.apply);
  const setStatus = useDebate((s) => s.setStatus);
  const [connected, setConnected] = React.useState(false);
  const handleRef = React.useRef<{ close: () => void } | null>(null);

  const connect = React.useCallback(() => {
    if (!sessionId || handleRef.current) return;
    setStatus("live");
    const handle = openSse(
      endpoints.v4_debate_stream(sessionId),
      {
        onOpen: () => setConnected(true),
        onError: () => {
          setConnected(false);
        },
        onMessage: (data) => {
          if (!data || typeof data !== "object") return;
          apply(data as DebateEvent);
        },
      },
      [],
    );
    handleRef.current = handle;
  }, [sessionId, apply, setStatus]);

  const disconnect = React.useCallback(() => {
    handleRef.current?.close();
    handleRef.current = null;
    setConnected(false);
  }, []);

  React.useEffect(() => {
    if (!autoStart) return;
    connect();
    return disconnect;
  }, [autoStart, connect, disconnect]);

  return { connected, connect, disconnect };
}
