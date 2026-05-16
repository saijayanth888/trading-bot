/**
 * Handwritten fallback types for `/api/v5/*` resources.
 *
 * Used when the codegen step in `scripts/codegen.sh` couldn't reach the
 * dashboard's `/openapi.json` (404 or backend down). Once codegen runs
 * successfully against a live backend, `src/types/api.ts` will contain the
 * authoritative schemas; consumers can switch imports over. Until then,
 * these structural types match spec §5.2 and let the hooks compile.
 *
 * owner: builder-D
 */

// -- shared meta envelope ----------------------------------------------------

/** Staleness/freshness envelope attached to every v5 card payload. */
export interface MetaEnvelope {
  age_s: number | null;
  stale: boolean;
  snapshot_ts: string | null;
  market_open_now: boolean | null;
}

export interface WithMeta<T> {
  _meta?: MetaEnvelope;
}

// -- /api/v5/status ----------------------------------------------------------

export type OperatorState = "green" | "amber" | "red";

export interface StatusPayload extends WithMeta<unknown> {
  state: OperatorState;
  banner: string; // e.g. "all clear" | "1 stale feed" | "single-name-cap breach"
  detect_counts: {
    info: number;
    warning: number;
    danger: number;
  };
  equity_usd: number;
  day_pnl_pct: number;
  clock_et: string; // "10:47 ET"
  _meta?: MetaEnvelope;
}

// -- /api/v5/portfolio -------------------------------------------------------

export interface SidePortfolio {
  equity_usd: number;
  peak_usd: number;
  drawdown_pct: number;
  day_pnl_usd: number;
  day_pnl_pct: number;
  sparkline_usd: Array<{ ts: string; v: number }>;
  _meta?: MetaEnvelope;
}

export interface PortfolioPayload {
  combined: SidePortfolio;
  crypto: SidePortfolio;
  stocks: SidePortfolio;
  pause_threshold_pct: number; // e.g. 3
  kill_threshold_pct: number; // e.g. 10
  _meta?: MetaEnvelope;
}

// -- /api/v5/positions -------------------------------------------------------

export interface Position {
  symbol: string;
  side: "crypto" | "stocks" | "shark";
  qty: number;
  avg_price: number;
  last_price: number | null;
  unrealised_pnl_usd: number;
  unrealised_pnl_pct: number;
  notional_usd: number;
  source: "fills" | "wheel" | "shark";
}

export interface PositionsPayload {
  positions: Position[];
  _meta?: MetaEnvelope;
}

// -- /api/v5/alerts ----------------------------------------------------------

export type AlertSeverity = "info" | "warning" | "danger";
export type AlertKind = "stale" | "gate-breach" | "risk-violation" | "b2" | "hermes" | "other";

export interface AlertItem {
  id: string;
  severity: AlertSeverity;
  kind: AlertKind;
  title: string;
  detail: string;
  ts: string;
  ack: boolean;
  _meta?: MetaEnvelope;
}

export interface AlertsPayload {
  items: AlertItem[];
  _meta?: MetaEnvelope;
}

// -- /api/v5/strategies/{kind} ----------------------------------------------

export type StrategyKind = "crypto-v4" | "stocks-wheel" | "shark";

export interface StrategyPayload {
  kind: StrategyKind;
  enabled: boolean;
  // Producer returns full regime row from regime_log/stock_regime — label,
  // confidence, and freshness. Not just the label.
  regime: { current: string; probability: number | null; ts: string | null } | null;
  equity_usd: number;
  day_pnl_usd: number;
  day_pnl_pct: number;
  open_positions: number;
  last_fill_ts: string | null;
  notes: string | null;
  _meta?: MetaEnvelope;
}

// -- /api/v5/metrics ---------------------------------------------------------

export interface MetricsPayload {
  sharpe: number | null;
  max_drawdown_pct: number | null;
  win_rate_pct: number | null;
  total_trades: number;
  walk_forward_windows: number;
  _meta?: MetaEnvelope;
}

// -- /api/v5/hermes/* --------------------------------------------------------

export interface HermesJob {
  id: string;
  cron: string;
  command: string;
  next_fire_ts: string | null;
  last_status: "ok" | "fail" | "running" | "unknown";
  last_run_ts: string | null;
}

export interface HermesSchedulePayload {
  jobs: HermesJob[];
  _meta?: MetaEnvelope;
}

export interface HermesRun {
  id: string;
  job_id: string;
  started_ts: string;
  finished_ts: string | null;
  status: "ok" | "fail" | "running";
  output_head: string;
  output_path: string;
}

export interface HermesRunsPayload {
  runs: HermesRun[];
  _meta?: MetaEnvelope;
}

export interface HermesHealthPayload {
  composite: "ok" | "degraded" | "down";
  gateway_age_s: number | null;
  mcp_age_s: number | null;
  dashboard_age_s: number | null;
  _meta?: MetaEnvelope;
}

// -- /api/v5/decisions -------------------------------------------------------

export interface DecisionRow {
  id: string;
  ts: string;
  symbol: string;
  side: "buy" | "sell";
  reason: string;
  strategy: string;
  pnl_usd: number | null;
}

export interface DecisionsPayload {
  rows: DecisionRow[];
  _meta?: MetaEnvelope;
}

// -- /api/v5/regime_config ---------------------------------------------------

export interface RegimeConfigPayload {
  crypto: {
    hmm_states: number;
    window_days: number;
    pause_drawdown_pct: number;
  };
  stocks: {
    iv_low_pct: number;
    iv_high_pct: number;
    earnings_blackout_days: number;
  };
  _meta?: MetaEnvelope;
}

// -- WS diff envelope --------------------------------------------------------

export interface WsDiff {
  path: string; // e.g. "/api/v5/portfolio"
  op: "replace" | "merge" | "delete";
  value: unknown;
  ts: string;
}
