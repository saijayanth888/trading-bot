/**
 * V5 realtime client.
 *
 * Strategy per spec §4.1 + §5.1:
 *   - Connect to `/api/v5/stream`. Diffs arrive as
 *     `{path, op, value, ts}` and are applied via `queryClient.setQueryData`.
 *   - On disconnect: flip global `wsState` atom to `polling`. TanStack Query
 *     keeps every hook on a 10s `refetchInterval` fallback, so data flows
 *     without WS.
 *   - Reconnect with exponential backoff (1s → 2s → 5s → 15s → 30s cap).
 *   - Expose `useWsState()` for `<TopBar>` so the operator sees the chip:
 *       connected → green dot, polling 10s → amber, offline → red.
 *
 * NOTE on diff `op` semantics:
 *   - `replace`  → `setQueryData(key, value)`
 *   - `merge`    → `setQueryData(key, prev => ({...prev, ...value}))`
 *   - `delete`   → `setQueryData(key, undefined)` (TanStack will refetch)
 *
 * owner: builder-D
 */

import { useSyncExternalStore } from "react";
import type { QueryClient } from "@tanstack/react-query";
import type { WsDiff } from "./types-fallback";

export type WsState = "connecting" | "connected" | "polling" | "offline";

// --- tiny pub/sub atom (avoids pulling zustand into ws-only code) -----------

type Listener = () => void;
let _state: WsState = "connecting";
const _listeners = new Set<Listener>();

function setWsState(next: WsState) {
  if (_state === next) return;
  _state = next;
  for (const l of _listeners) l();
}

export function getWsState(): WsState {
  return _state;
}

export function useWsState(): WsState {
  return useSyncExternalStore(
    (cb) => {
      _listeners.add(cb);
      return () => _listeners.delete(cb);
    },
    getWsState,
    getWsState,
  );
}

// --- diff → query-cache application -----------------------------------------

/**
 * Convert a stream `path` like `/api/v5/portfolio` to a TanStack query key.
 * Hooks register under `["v5", <resource>, ...args]` so server diffs map back.
 */
function pathToQueryKey(path: string): unknown[] | null {
  // Accept both `/api/v5/foo` and `/api/v5/foo/bar?x=1` shapes.
  const m = path.match(/^\/api\/v5\/([^?]+)(?:\?(.+))?$/);
  if (!m) return null;
  const segs = m[1].split("/").filter(Boolean);
  // strategies/crypto-v4 → ["v5", "strategies", "crypto-v4"]
  // hermes/runs → ["v5", "hermes", "runs"]
  // portfolio → ["v5", "portfolio"]
  return ["v5", ...segs];
}

function applyDiff(qc: QueryClient, diff: WsDiff) {
  const key = pathToQueryKey(diff.path);
  if (!key) return;
  switch (diff.op) {
    case "replace":
      qc.setQueryData(key, diff.value);
      break;
    case "merge":
      qc.setQueryData(key, (prev: unknown) => {
        if (prev && typeof prev === "object" && diff.value && typeof diff.value === "object") {
          return { ...(prev as object), ...(diff.value as object) };
        }
        return diff.value;
      });
      break;
    case "delete":
      qc.setQueryData(key, undefined);
      qc.invalidateQueries({ queryKey: key });
      break;
  }
}

// --- connection lifecycle ---------------------------------------------------

const BACKOFF_MS = [1000, 2000, 5000, 15000, 30000];
let _socket: WebSocket | null = null;
let _attempt = 0;
let _started = false;
let _reconnectTimer: ReturnType<typeof setTimeout> | null = null;

function wsUrl(): string {
  if (typeof window === "undefined") return "";
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/v5/stream`;
}

function scheduleReconnect(qc: QueryClient) {
  if (_reconnectTimer) return;
  const delay = BACKOFF_MS[Math.min(_attempt, BACKOFF_MS.length - 1)];
  _attempt += 1;
  setWsState("polling"); // visible degradation while we wait
  _reconnectTimer = setTimeout(() => {
    _reconnectTimer = null;
    connectOnce(qc);
  }, delay);
}

function connectOnce(qc: QueryClient) {
  if (typeof window === "undefined") return;
  setWsState("connecting");
  let sock: WebSocket;
  try {
    sock = new WebSocket(wsUrl());
  } catch {
    scheduleReconnect(qc);
    return;
  }
  _socket = sock;

  sock.addEventListener("open", () => {
    _attempt = 0;
    setWsState("connected");
  });

  sock.addEventListener("message", (ev) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(ev.data as string);
    } catch {
      return;
    }
    if (!parsed || typeof parsed !== "object") return;
    const diff = parsed as Partial<WsDiff>;
    if (typeof diff.path !== "string" || typeof diff.op !== "string") return;
    applyDiff(qc, diff as WsDiff);
  });

  sock.addEventListener("close", () => {
    if (_socket === sock) _socket = null;
    scheduleReconnect(qc);
  });

  sock.addEventListener("error", () => {
    // Browsers fire `error` followed by `close`; let `close` schedule.
    try {
      sock.close();
    } catch {
      /* already closed */
    }
  });
}

/**
 * Idempotent start. Call once from `main.tsx` after the QueryClient is built.
 * Subsequent calls are no-ops.
 */
export function startWs(qc: QueryClient): void {
  if (_started) return;
  _started = true;
  connectOnce(qc);

  if (typeof window !== "undefined") {
    window.addEventListener("offline", () => setWsState("offline"));
    window.addEventListener("online", () => {
      // force an immediate reconnect attempt on connectivity restore
      if (_reconnectTimer) {
        clearTimeout(_reconnectTimer);
        _reconnectTimer = null;
      }
      _attempt = 0;
      connectOnce(qc);
    });
    // visibility-change: don't tear down, just nudge a reconnect if hidden→visible
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && _state !== "connected") {
        if (_reconnectTimer) {
          clearTimeout(_reconnectTimer);
          _reconnectTimer = null;
        }
        _attempt = 0;
        connectOnce(qc);
      }
    });
  }
}

/** For tests/teardown only. */
export function _stopWsForTest(): void {
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  if (_socket) {
    try {
      _socket.close();
    } catch {
      /* */
    }
    _socket = null;
  }
  _started = false;
  _attempt = 0;
  setWsState("connecting");
}
