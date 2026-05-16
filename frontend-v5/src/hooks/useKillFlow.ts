/**
 * Kill-switch state machine.
 *
 *   idle ─arm()→ armed ─openModal()→ confirming
 *     ↑                                   │
 *     │                                   │ user types "KILL" → enables Confirm
 *     │                                   │ Confirm pressed
 *     │                                   ▼
 *     └────cancel()─── executing ──→ done | cancelled
 *                          (POST /api/v5/actions/kill)
 *
 * Per spec §4.3 + frontend-debate G8:
 *   - Modal traps focus on the textbox, NOT the Confirm button.
 *   - Confirm stays disabled until the operator types literal "KILL" (uppercase).
 *   - Single confirm, no auditory alarm.
 *
 * Caller wires:
 *   const kill = useKillFlow();
 *   <KillAllButton onClick={kill.arm} state={kill.state} />
 *   <KillConfirmModal
 *       open={kill.state === 'confirming'}
 *       typed={kill.typed}
 *       canConfirm={kill.canConfirm}
 *       onType={kill.setTyped}
 *       onConfirm={kill.confirm}
 *       onCancel={kill.cancel}
 *   />
 *
 * owner: builder-D
 */
import { useCallback, useRef, useState } from "react";
import { ApiError, apiPost, endpoints } from "@/lib/api";

export type KillState =
  | "idle"
  | "armed"
  | "confirming"
  | "executing"
  | "done"
  | "cancelled";

const CONFIRM_PHRASE = "KILL";

export interface UseKillFlow {
  state: KillState;
  typed: string;
  canConfirm: boolean;
  error: string | null;
  /** idle → confirming (skip "armed" linger; spec uses it as transient). */
  arm: () => void;
  /** confirming → idle/cancelled (textbox close). */
  cancel: () => void;
  /** Update the type-to-confirm textbox contents. */
  setTyped: (next: string) => void;
  /** Fire the kill (only if canConfirm). */
  confirm: () => Promise<void>;
  /** Reset to idle after a done/cancelled cycle. */
  reset: () => void;
}

export function useKillFlow(): UseKillFlow {
  const [state, setState] = useState<KillState>("idle");
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const lastFireAtRef = useRef<number>(0);

  const canConfirm =
    state === "confirming" &&
    typed.trim().toUpperCase() === CONFIRM_PHRASE;

  const arm = useCallback(() => {
    setError(null);
    setTyped("");
    // armed → confirming in the same tick keeps the modal predictable
    // for keyboard users; spec calls these states out separately for
    // testability + accessibility.
    setState("armed");
    queueMicrotask(() => setState("confirming"));
  }, []);

  const cancel = useCallback(() => {
    setState("cancelled");
    setTyped("");
    // Quickly return to idle so subsequent arm() works.
    queueMicrotask(() => setState("idle"));
  }, []);

  const reset = useCallback(() => {
    setState("idle");
    setTyped("");
    setError(null);
  }, []);

  const confirm = useCallback(async () => {
    if (state !== "confirming") return;
    if (typed.trim().toUpperCase() !== CONFIRM_PHRASE) return;
    // Debounce: at most one fire per 2s to defeat double-clicks.
    const now = Date.now();
    if (now - lastFireAtRef.current < 2000) return;
    lastFireAtRef.current = now;

    setState("executing");
    setError(null);
    try {
      await apiPost(endpoints.v5_action_kill, { source: "operator-console", typed });
      setState("done");
      setTyped("");
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.status} ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(msg);
      // On failure, drop back to confirming so the operator can retry or cancel.
      setState("confirming");
    }
  }, [state, typed]);

  return {
    state,
    typed,
    canConfirm,
    error,
    arm,
    cancel,
    setTyped,
    confirm,
    reset,
  };
}

/**
 * Helper for pause/flatten — simpler, no type-to-confirm.
 * Returns a `fire(arg)` callback that POSTs and surfaces error+busy state.
 */
export function useStrategyAction(action: "pause" | "flatten") {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fire = useCallback(
    async (arg: string) => {
      setBusy(true);
      setError(null);
      try {
        const url =
          action === "pause"
            ? endpoints.v5_action_pause(arg)
            : endpoints.v5_action_flatten(arg);
        await apiPost(url);
      } catch (err) {
        setError(err instanceof Error ? err.message : "unknown error");
      } finally {
        setBusy(false);
      }
    },
    [action],
  );

  return { fire, busy, error };
}
