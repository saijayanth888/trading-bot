import { create } from "zustand";
import type { AgentVote, ArbiterSummary, DebateEvent, DebateSession } from "@/types/v4";

interface DebateState {
  /** Currently watched session (live or replay). */
  session: DebateSession | null;
  /** Per-role partial token accumulators while votes are streaming. */
  partials: Record<string, string>;
  /** "live" while EventSource is open, "complete" / "aborted" / "idle" otherwise. */
  status: "idle" | "live" | "complete" | "aborted";
  setSession: (s: DebateSession | null) => void;
  setStatus: (s: DebateState["status"]) => void;
  apply: (ev: DebateEvent) => void;
  reset: () => void;
}

export const useDebate = create<DebateState>((set, get) => ({
  session: null,
  partials: {},
  status: "idle",
  setSession: (s) => set({ session: s, partials: {}, status: s ? "complete" : "idle" }),
  setStatus: (s) => set({ status: s }),
  reset: () => set({ session: null, partials: {}, status: "idle" }),
  apply: (ev) => {
    const cur = get().session;
    switch (ev.kind) {
      case "session_start":
        set({
          session: {
            session_id: ev.session_id,
            pair: ev.pair,
            setup_ts: ev.setup_ts,
            status: "running",
            votes: [],
          },
          partials: {},
          status: "live",
        });
        return;
      case "vote_partial": {
        const partials = { ...get().partials };
        partials[ev.role] = (partials[ev.role] || "") + ev.token;
        set({ partials });
        return;
      }
      case "vote_complete": {
        if (!cur) return;
        const filtered = cur.votes.filter((v) => v.role !== ev.vote.role);
        const partials = { ...get().partials };
        delete partials[ev.vote.role];
        set({
          session: { ...cur, votes: [...filtered, ev.vote] },
          partials,
        });
        return;
      }
      case "arbiter":
        if (!cur) return;
        set({ session: { ...cur, arbiter: ev.arbiter } });
        return;
      case "decision":
        if (!cur) return;
        set({
          session: {
            ...cur,
            aggregate: ev.aggregate,
            decision: ev.decision,
            total_latency_ms: ev.total_latency_ms,
            status: "complete",
          },
          status: "complete",
        });
        return;
      case "abort":
        if (!cur) return;
        set({
          session: { ...cur, status: "aborted" },
          status: "aborted",
        });
        return;
      case "heartbeat":
        return;
    }
  },
}));

export type { AgentVote, ArbiterSummary };
