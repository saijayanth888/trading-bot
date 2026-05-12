/**
 * V4 type contracts — mirrors the JSON schemas defined in
 * docs/quanta-core-v4/06-ARCHITECTURE.md and 05-RESEARCH-PARALLEL_AGENTS.md.
 *
 * These are intentionally narrow — the backend may return additional fields
 * (forwards-compatible), but everything the UI reads MUST be present.
 */

// ---------- Debate ----------

export type DebateRole = "regime" | "micro" | "bull" | "bear" | "arbiter" | "reflector";
export type DebateVote = "LONG" | "SHORT" | "FLAT" | "ABSTAIN";

export interface AgentVote {
  role: DebateRole;
  model: string;          // e.g. "hermes3:8b" / "hermes3:70b"
  vote: DebateVote;
  conviction: number;     // 0..1
  rationale: string;      // 0..n paragraphs
  evidence_keys: string[];
  latency_ms: number;
  emitted_at: string;     // ISO8601
}

export interface ArbiterSummary {
  synthesized_action: DebateVote;
  synthesis_rationale: string;
  agreement_pattern: string;
  dissent_notes: string[];
}

export interface DebateAggregateScore {
  score: number;
  n_valid: number;
  consensus: boolean;
  method: string;         // "weighted_vote" | "veto_quorum" | "veto_risk_engine" | …
}

export interface DebateSession {
  session_id: string;
  pair: string;
  setup_ts: string;
  status: "running" | "complete" | "aborted";
  votes: AgentVote[];
  arbiter?: ArbiterSummary;
  aggregate?: DebateAggregateScore;
  decision?: DebateVote;
  total_latency_ms?: number;
}

// ---------- Debate streaming events ----------

export type DebateEvent =
  | { kind: "session_start"; session_id: string; pair: string; setup_ts: string }
  | { kind: "vote_partial"; role: DebateRole; token: string }
  | { kind: "vote_complete"; vote: AgentVote }
  | { kind: "arbiter"; arbiter: ArbiterSummary }
  | { kind: "decision"; aggregate: DebateAggregateScore; decision: DebateVote; total_latency_ms: number }
  | { kind: "abort"; reason: string }
  | { kind: "heartbeat"; ts: string };

// ---------- Monte Carlo ----------

export interface MontecarloPath {
  /** Sampled equity curve, normalized to entry = 1.0 */
  values: number[];
}

export interface MontecarloRun {
  trade_id: string;
  pair: string;
  side: "LONG" | "SHORT";
  n_paths: number;
  horizon_bars: number;
  /** Subset of paths returned to the UI for visualisation (full N is server-side). */
  sample_paths: MontecarloPath[];
  /** Per-bar quantile envelopes — length = horizon_bars + 1 each. */
  quantiles: { p05: number[]; p25: number[]; p50: number[]; p75: number[]; p95: number[] };
  var_95: number;
  expected_shortfall_95: number;
  blocked: boolean;
  block_reason?: string;
}

// ---------- Adapters / LoRA ----------

export interface AdapterRecord {
  id: string;
  role: DebateRole;
  base_model: string;
  promoted_at: string;
  faithfulness: number;     // 0..1
  hit_rate: number;         // 0..1
  pareto_dominated: boolean;
  status: "champion" | "pareto" | "rolled_back" | "candidate";
  notes?: string;
}

// ---------- Weekly preview ----------

export interface WeeklyPreview {
  iso_week: string;          // "2026-19"
  monday: string;
  sunday: string;
  generated_ts: string;
  net_pnl: number;
  net_pnl_pct: number;
  drawdown_pct: number;
  open_count: number;
  trade_count: number;
  run_mode: "paper" | "live";
  /** Rendered Markdown — server already runs the Jinja template. */
  markdown: string;
}

// ---------- Backtest parity ----------

export interface ParityRow {
  ts: string;                // ISO of the bar
  pair: string;
  live_action: "LONG" | "SHORT" | "FLAT";
  backtest_action: "LONG" | "SHORT" | "FLAT";
  live_pnl: number | null;
  backtest_pnl: number | null;
  divergent: boolean;
}

export interface ParitySummary {
  rows: ParityRow[];
  weeks: { iso: string; divergence_pct: number }[];
  consecutive_days_ok: number;
  cutover_threshold_days: number;   // typically 14
}

// ---------- Screening grid ----------

export interface ScreeningName {
  symbol: string;
  asset_class: "crypto" | "stock";
  regime: string;
  detected: boolean;          // setup formed
  converged: boolean;         // panel reached unanimity
  traded: boolean;            // actually filled
  last_setup_ts?: string;
  thesis?: string;
}

export interface ScreeningSnapshot {
  generated_ts: string;
  names: ScreeningName[];
  funnel: {
    detected: number;
    converged: number;
    traded: number;
  };
}
