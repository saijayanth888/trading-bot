// owner: builder-C  (data wiring added by builder-D — layout untouched)
// App-level layout per spec §3 / §4.2. Heavy panels are lazy-imported per
// frontend-debate G7 + spec §4.1 (Vite code-splitting boundaries).
import { lazy, Suspense, useMemo } from "react";
import { TopBar } from "./components/TopBar";
import { CapitalCard } from "./components/Monitor/CapitalCard";
import { DrawdownRibbon } from "./components/Monitor/DrawdownRibbon";
import { RegimeChip } from "./components/Monitor/RegimeChip";
import { DetectFeed } from "./components/Detect/DetectFeed";
import { Intervene } from "./components/Intervene";

// builder-D: data hooks (one call per resource; refetchInterval=10s inside)
import { useStatus } from "./hooks/useStatus";
import { usePortfolio } from "./hooks/usePortfolio";
import { useAlerts } from "./hooks/useAlerts";
import { useStrategies } from "./hooks/useStrategies";
import { useMetrics } from "./hooks/useMetrics";
import { useHermes, useHermesRetrigger } from "./hooks/useHermes";
import { useRegimeConfig } from "./hooks/useRegimeConfig";
import { useDecisions } from "./hooks/useDecisions";
import { useNumberRollTrigger } from "./hooks/useNumberRollTrigger";
import { useStrategyAction, useKillFlow } from "./hooks/useKillFlow";
import { useWsState } from "./lib/ws";
import { apiPost, endpoints } from "./lib/api";
import type { AlertItemData } from "./components/Detect/AlertItem";

const StrategyStrip = lazy(() =>
  import("./components/Strategies/StrategyStrip").then((m) => ({
    default: m.StrategyStrip,
  })),
);
const HermesPanel = lazy(() =>
  import("./components/Integrations/HermesPanel").then((m) => ({
    default: m.HermesPanel,
  })),
);
const ModelForgeSideLink = lazy(() =>
  import("./components/Integrations/ModelForgeSideLink").then((m) => ({
    default: m.ModelForgeSideLink,
  })),
);
const RegimeConfigEditor = lazy(() =>
  import("./components/RegimeConfigEditor").then((m) => ({
    default: m.RegimeConfigEditor,
  })),
);
const DecisionAudit = lazy(() =>
  import("./components/DecisionAudit").then((m) => ({
    default: m.DecisionAudit,
  })),
);
const MCPConsole = lazy(() =>
  import("./components/MCPConsole").then((m) => ({ default: m.MCPConsole })),
);

function PanelFallback({ label }: { label: string }) {
  return (
    <div className="rounded border border-stroke-1 bg-bg-card p-4 text-text-3 text-xs">
      loading {label}…
    </div>
  );
}

export default function App() {
  // --- builder-D wiring ------------------------------------------------------
  const status = useStatus();
  const portfolio = usePortfolio();
  const alerts = useAlerts();
  const strategies = useStrategies();
  const metrics = useMetrics();
  const hermes = useHermes();
  const retrigger = useHermesRetrigger();
  const regimeConfig = useRegimeConfig();
  const decisions = useDecisions(50);
  const wsState = useWsState();
  const pause = useStrategyAction("pause");
  const topBarKill = useKillFlow(); // top-bar KILL button mirror

  const polling = wsState !== "connected";

  // Top-bar status → ('clear'|'watch'|'halt'|'unknown') mapping
  const topBarStatus =
    status.data?.state === "green"
      ? "clear"
      : status.data?.state === "amber"
        ? "watch"
        : status.data?.state === "red"
          ? "halt"
          : "unknown";

  // Per spec §4.3: NumberRoll only on specific triggers
  const capitalTierTrigger = useNumberRollTrigger(
    portfolio.data?.combined.equity_usd ?? null,
    "capital-tier",
  );
  const ddThresholdTrigger = useNumberRollTrigger(
    portfolio.data?.combined.drawdown_pct ?? null,
    "dd-threshold",
    {
      pausePct: portfolio.data?.pause_threshold_pct ?? 3,
      killPct: portfolio.data?.kill_threshold_pct ?? 10,
    },
  );

  // Sparkline transform
  const sparkline = useMemo(
    () =>
      (portfolio.data?.combined.sparkline_usd ?? []).map((p, i) => ({
        t: i,
        v: p.v,
      })),
    [portfolio.data],
  );

  // Map v5 alerts to AlertItemData (severity strings differ: server uses
  // "warning"/"danger", UI uses "warn"/"danger").
  const alertRows: AlertItemData[] = useMemo(() => {
    const sevMap = { info: "info", warning: "warn", danger: "danger" } as const;
    const kindMap = {
      stale: "stale-feed",
      "gate-breach": "gate-breach",
      "risk-violation": "risk-violation",
      b2: "b2-class",
      hermes: "other",
      other: "other",
    } as const;
    return (alerts.data?.items ?? []).map((a) => ({
      id: a.id,
      severity: sevMap[a.severity] ?? "info",
      kind: kindMap[a.kind] ?? "other",
      title: a.title,
      detail: a.detail,
      ts: a.ts,
      meta: a._meta ?? null,
    }));
  }, [alerts.data]);

  return (
    <div className="min-h-screen bg-bg-page text-text-1">
      <TopBar
        status={topBarStatus}
        statusText={status.data?.banner ?? "loading…"}
        capitalUsd={portfolio.data?.combined.equity_usd ?? null}
        dayPnlPct={portfolio.data?.combined.day_pnl_pct ?? null}
        wsUp={wsState === "connected"}
        pollIntervalS={10}
        onKill={topBarKill.arm}
      />

      <main className="mx-auto max-w-[1920px] px-6 py-4 space-y-6 pb-32">
        {/* MONITOR — scrolls, NOT sticky (per frontend-debate G2) */}
        <section
          aria-label="monitor"
          className="grid grid-cols-12 gap-4 lg:grid-cols-12"
        >
          <div className="col-span-12 lg:col-span-5">
            <CapitalCard
              equityUsd={portfolio.data?.combined.equity_usd ?? null}
              dayPnlUsd={portfolio.data?.combined.day_pnl_usd ?? null}
              dayPnlPct={portfolio.data?.combined.day_pnl_pct ?? null}
              sparkline={sparkline}
              meta={portfolio.data?.combined._meta ?? portfolio.data?._meta ?? null}
              polling={polling}
              key={capitalTierTrigger ? "tier-cross" : "stable"}
            />
          </div>
          <div className="col-span-12 lg:col-span-4">
            <DrawdownRibbon
              ddPct={portfolio.data?.combined.drawdown_pct ?? null}
              pausePct={portfolio.data?.pause_threshold_pct ?? 3}
              killPct={portfolio.data?.kill_threshold_pct ?? 10}
              crossedThreshold={ddThresholdTrigger}
              meta={portfolio.data?.combined._meta ?? portfolio.data?._meta ?? null}
              polling={polling}
            />
          </div>
          <div className="col-span-12 lg:col-span-3 space-y-2">
            <RegimeChip
              kind="crypto"
              regime={strategies.crypto.data?.regime?.current ?? null}
              confidence={strategies.crypto.data?.regime?.probability ?? null}
              meta={strategies.crypto.data?._meta ?? null}
              polling={polling}
            />
            <RegimeChip
              kind="stocks"
              regime={strategies.stocks.data?.regime?.current ?? null}
              confidence={strategies.stocks.data?.regime?.probability ?? null}
              meta={strategies.stocks.data?._meta ?? null}
              polling={polling}
            />
          </div>
        </section>

        {/* DETECT */}
        <section aria-label="detect">
          <DetectFeed
            alerts={alertRows}
            meta={alerts.data?._meta ?? null}
            polling={polling}
            onAck={(id) => {
              // Optimistic: fire-and-forget ack via legacy hermes acks endpoint
              void apiPost(`/api/v5/alerts/${id}/ack`).catch(() => {});
            }}
          />
        </section>

        {/* STRATEGIES */}
        <section aria-label="strategies" className="space-y-2">
          <Suspense fallback={<PanelFallback label="strategies" />}>
            <StrategyStrip
              kind="crypto-v4"
              equityUsd={strategies.crypto.data?.equity_usd ?? null}
              dayPnlPct={strategies.crypto.data?.day_pnl_pct ?? null}
              openPositions={strategies.crypto.data?.open_positions ?? null}
              sharpe={metrics.data?.sharpe ?? null}
              winRatePct={metrics.data?.win_rate_pct ?? null}
              regime={strategies.crypto.data?.regime?.current ?? null}
              status={
                strategies.crypto.data == null
                  ? "unknown"
                  : strategies.crypto.data.enabled
                    ? "running"
                    : "paused"
              }
              meta={strategies.crypto.data?._meta ?? null}
              polling={polling}
              onPause={(k) => pause.fire(k)}
            />
            <StrategyStrip
              kind="stocks-wheel"
              equityUsd={strategies.stocks.data?.equity_usd ?? null}
              dayPnlPct={strategies.stocks.data?.day_pnl_pct ?? null}
              openPositions={strategies.stocks.data?.open_positions ?? null}
              sharpe={metrics.data?.sharpe ?? null}
              winRatePct={metrics.data?.win_rate_pct ?? null}
              regime={strategies.stocks.data?.regime?.current ?? null}
              status={
                strategies.stocks.data == null
                  ? "unknown"
                  : strategies.stocks.data.enabled
                    ? "running"
                    : "paused"
              }
              meta={strategies.stocks.data?._meta ?? null}
              polling={polling}
              onPause={(k) => pause.fire(k)}
            />
            <StrategyStrip
              kind="shark"
              equityUsd={strategies.shark.data?.equity_usd ?? null}
              dayPnlPct={strategies.shark.data?.day_pnl_pct ?? null}
              openPositions={strategies.shark.data?.open_positions ?? null}
              sharpe={metrics.data?.sharpe ?? null}
              winRatePct={metrics.data?.win_rate_pct ?? null}
              regime={strategies.shark.data?.regime?.current ?? null}
              status={
                strategies.shark.data == null
                  ? "unknown"
                  : strategies.shark.data.enabled
                    ? "running"
                    : "paused"
              }
              meta={strategies.shark.data?._meta ?? null}
              polling={polling}
              onPause={(k) => pause.fire(k)}
            />
          </Suspense>
        </section>

        {/* INTEGRATIONS */}
        <section
          aria-label="integrations"
          className="grid grid-cols-12 gap-4"
        >
          <div className="col-span-12 lg:col-span-8">
            <Suspense fallback={<PanelFallback label="hermes" />}>
              <HermesPanel
                schedule={(hermes.schedule.data?.jobs ?? []).map((j) => ({
                  id: j.id,
                  cron: j.cron,
                  nextFireTs: j.next_fire_ts,
                  lastStatus: j.last_status,
                  lastRunTs: j.last_run_ts,
                }))}
                runs={(hermes.runs.data?.runs ?? []).map((r) => ({
                  id: r.id,
                  jobId: r.job_id,
                  startedTs: r.started_ts,
                  outcome: r.status,
                  tail: r.output_head,
                }))}
                healthOk={
                  hermes.health.data == null
                    ? null
                    : hermes.health.data.composite === "ok"
                }
                meta={hermes.schedule.data?._meta ?? null}
                polling={polling}
                onRetrigger={(id) => retrigger.mutate(id)}
              />
            </Suspense>
          </div>
          <div className="col-span-12 lg:col-span-4">
            <Suspense fallback={<PanelFallback label="modelforge" />}>
              <ModelForgeSideLink />
            </Suspense>
          </div>
        </section>

        {/* COLLAPSED-BY-DEFAULT — per spec §3 + operator scope on G3 */}
        <section aria-label="advanced" className="space-y-2">
          <Suspense fallback={<PanelFallback label="regime config" />}>
            <RegimeConfigEditor
              config={regimeConfig.data as Record<string, unknown> | undefined}
              onSave={(patch) => {
                void apiPost(endpoints.v5_regime_config, patch);
              }}
            />
          </Suspense>
          <Suspense fallback={<PanelFallback label="decision audit" />}>
            <DecisionAudit
              decisions={(decisions.data?.rows ?? []).map((d) => ({
                id: d.id,
                ts: d.ts,
                symbol: d.symbol,
                side: d.side,
                reason: d.reason,
                pnlUsd: d.pnl_usd,
              }))}
            />
          </Suspense>
          <Suspense fallback={<PanelFallback label="mcp console" />}>
            <MCPConsole
              tools={MCP_TOOLS}
              onInvoke={async (tool, args) =>
                apiPost(endpoints.v5_mcp(tool), args)
              }
            />
          </Suspense>
        </section>
      </main>

      {/* INTERVENE — position:fixed bottom-right, NOT a band (G2) */}
      <Intervene />
    </div>
  );
}

const MCP_TOOLS = [
  "shark_briefing",
  "regime_refresh",
  "kb_refresh_daily",
  "weekly_post_mortem",
  "single_name_cap_audit",
  "vllm_health",
  "hermes_heartbeat",
  "spark_memory_guard",
];
