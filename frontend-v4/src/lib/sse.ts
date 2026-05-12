/**
 * Minimal SSE helper. EventSource lacks an onclose hook that distinguishes
 * "stream ended" from "server died" — we wrap it to surface both states to
 * the consumer.
 */

export type SseHandler = {
  onMessage?: (data: unknown, rawEvent: MessageEvent) => void;
  onNamed?: (event: string, data: unknown) => void;
  onError?: (err: Event) => void;
  onOpen?: () => void;
};

export type SseHandle = {
  close: () => void;
};

export function openSse(url: string, handler: SseHandler, namedEvents: string[] = []): SseHandle {
  const es = new EventSource(url);

  if (handler.onOpen) es.addEventListener("open", handler.onOpen);

  es.onmessage = (ev) => {
    if (!handler.onMessage) return;
    let parsed: unknown = ev.data;
    if (typeof ev.data === "string" && ev.data.length > 0) {
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        /* keep raw */
      }
    }
    handler.onMessage(parsed, ev);
  };

  if (handler.onError) es.onerror = handler.onError;

  for (const name of namedEvents) {
    es.addEventListener(name, (ev) => {
      if (!handler.onNamed) return;
      const msg = ev as MessageEvent;
      let parsed: unknown = msg.data;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        /* keep raw */
      }
      handler.onNamed(name, parsed);
    });
  }

  return {
    close: () => es.close(),
  };
}
