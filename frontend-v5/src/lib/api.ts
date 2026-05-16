/**
 * V5 API client.
 *
 * Routing rules (spec §5.1):
 *   - `/api/v5/*`  → raw JSON, RFC 7807 problem-detail on error.
 *   - `/api/ops/*` and `/api/v4/*` → legacy `{status, data, error, checked_at}`
 *     envelope; we auto-unwrap and surface `error` as ApiError so TanStack
 *     Query treats it as a failed fetch (matches v4 ef7b212 fix).
 *
 * In dev (vite :5174) the proxy in `vite.config.ts` forwards /api/* to the
 * FastAPI dashboard (:8081). In prod the SPA is served from `/` on the same
 * host, so relative paths Just Work.
 *
 * owner: builder-D
 */

const BASE = ""; // relative — see header

export class ApiError extends Error {
  status: number;
  body?: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

function isLegacyPath(path: string): boolean {
  return path.startsWith("/api/ops/") || path.startsWith("/api/v4/");
}

interface Envelope {
  status?: string;
  data: unknown;
  error?: unknown;
  checked_at?: unknown;
}

function looksLikeEnvelope(body: unknown): body is Envelope {
  return (
    body !== null &&
    typeof body === "object" &&
    "status" in body &&
    "data" in body &&
    "error" in body
  );
}

function unwrapEnvelope<T>(body: unknown, httpStatus: number): T {
  if (looksLikeEnvelope(body)) {
    if (body.error) {
      throw new ApiError(httpStatus, String(body.error), body);
    }
    return body.data as T;
  }
  return body as T;
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
      /* not JSON */
    }
    throw new ApiError(res.status, `${res.status} ${res.statusText} on ${path}`, body);
  }
  const body = await res.json();
  // v5 routes return raw; legacy routes return enveloped. We *always* try to
  // unwrap so a v5 endpoint that accidentally wraps still works, but only
  // legacy routes are guaranteed-enveloped.
  if (isLegacyPath(path)) {
    return unwrapEnvelope<T>(body, res.status);
  }
  // v5: if it *happens* to be enveloped (transitional), still unwrap; else raw.
  if (looksLikeEnvelope(body)) {
    return unwrapEnvelope<T>(body, res.status);
  }
  return body as T;
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
      /* not JSON */
    }
    throw new ApiError(res.status, `${res.status} ${res.statusText} on ${path}`, respBody);
  }
  // POST responses are not enveloped on v5; legacy mutating routes proxy to v5
  // (spec §5.3) so the response body is already the v5 shape.
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// Endpoint catalog — spec §5.2.
export const endpoints = {
  v5_status: "/api/v5/status",
  v5_portfolio: "/api/v5/portfolio",
  v5_positions: "/api/v5/positions",
  v5_alerts: "/api/v5/alerts",
  v5_metrics: "/api/v5/metrics",
  v5_strategy: (kind: string) => `/api/v5/strategies/${kind}`,
  v5_hermes_schedule: "/api/v5/hermes/schedule",
  v5_hermes_runs: (limit = 20) => `/api/v5/hermes/runs?limit=${limit}`,
  v5_hermes_health: "/api/v5/hermes/health",
  v5_regime_config: "/api/v5/regime_config",
  v5_decisions: (limit = 50) => `/api/v5/decisions?limit=${limit}`,
  v5_mcp: (tool: string) => `/api/v5/mcp/${tool}`,
  v5_ws: "/api/v5/stream",
  v5_action_kill: "/api/v5/actions/kill",
  v5_action_pause: (kind: string) => `/api/v5/actions/pause/${kind}`,
  v5_action_flatten: (symbol: string) => `/api/v5/actions/flatten/${symbol}`,
  v5_action_hermes_retrigger: (job: string) => `/api/v5/actions/hermes/retrigger/${job}`,
} as const;
