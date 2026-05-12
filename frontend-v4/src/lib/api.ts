/**
 * V4 API client.
 *
 * In dev (vite :5173) Vite proxies /api/* to the FastAPI dashboard at :8081.
 * In production the SPA is served from /v4/ on the same host, so relative
 * paths Just Work.
 */

const BASE = ""; // intentionally empty — relative paths

export class ApiError extends Error {
  status: number;
  body?: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export async function apiGet<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const url = `${BASE}${path}`;
  const res = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, `${res.status} ${res.statusText} on ${path}`, body);
  }
  return (await res.json()) as T;
}

export async function apiPost<T = unknown>(
  path: string,
  body?: unknown,
  init?: RequestInit,
): Promise<T> {
  const url = `${BASE}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(init?.headers || {}),
    },
    body: body == null ? undefined : JSON.stringify(body),
    ...init,
  });
  if (!res.ok) {
    let respBody: unknown;
    try {
      respBody = await res.json();
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, `${res.status} ${res.statusText} on ${path}`, respBody);
  }
  return (await res.json()) as T;
}

// Endpoint catalog — all routes are read-only unless noted.
export const endpoints = {
  // existing dashboard endpoints (server.py + ops_routes.py)
  universe: "/api/universe",
  state: "/api/state",
  pairs: "/api/pairs",
  regime: "/api/ops/regime",
  sentiment: "/api/ops/sentiment",
  trades_risk: "/api/ops/trades_risk",
  combined_portfolio: "/api/ops/combined_portfolio",
  llm_stats: "/api/ops/llm_stats",
  llm_calls: "/api/ops/llm_calls",
  circuit_breakers: "/api/ops/circuit_breakers",
  training: "/api/ops/training",
  services: "/api/ops/services",
  readiness: "/api/ops/readiness",
  gates: "/api/ops/gates",
  market_hours: "/api/ops/market_hours",
  stocks_ml: "/api/ops/stocks_ml",
  shark_briefing: "/api/ops/shark_briefing",

  // NEW V4 surfaces — see user_data/dashboard/v4_routes.py
  v4_debate_history: "/api/v4/debate/history",
  v4_debate_stream: (sessionId: string) => `/api/v4/debate/stream/${sessionId}`,
  v4_montecarlo: (tradeId: string) => `/api/v4/montecarlo/${tradeId}`,
  v4_adapters: "/api/v4/adapters",
  v4_adapter_rollback: (id: string) => `/api/v4/adapters/${id}/rollback`,
  v4_weekly_preview: "/api/v4/weekly/preview",
  v4_parity: "/api/v4/parity",
  v4_screening: "/api/v4/screening",
} as const;
