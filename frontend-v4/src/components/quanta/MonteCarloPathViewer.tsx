import * as React from "react";
import {
  Area,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useQuery } from "@tanstack/react-query";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { Card, CardHeader, CardBody, CardFooter } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Button } from "@/components/ui/button";
import { Stat } from "@/components/ui/stat";
import { apiGet, endpoints } from "@/lib/api";
import type { MontecarloRun } from "@/types/v4";
import { fmtPct } from "@/lib/format";

interface MonteCarloPathViewerProps {
  tradeId: string;
  /** When the parent already has the run, skip the query. */
  prefetched?: MontecarloRun;
}

export function MonteCarloPathViewer({ tradeId, prefetched }: MonteCarloPathViewerProps) {
  const [animatedIdx, setAnimatedIdx] = React.useState(0);

  const q = useQuery({
    queryKey: ["mc", tradeId],
    queryFn: () => apiGet<MontecarloRun>(endpoints.v4_montecarlo(tradeId)),
    enabled: !prefetched,
    refetchInterval: false,
  });

  const run: MontecarloRun | undefined = prefetched ?? q.data;

  // Animate path-by-path drawing — every 60ms reveal one more path.
  React.useEffect(() => {
    if (!run || run.sample_paths.length === 0) {
      setAnimatedIdx(0);
      return;
    }
    setAnimatedIdx(0);
    const handle = window.setInterval(() => {
      setAnimatedIdx((prev) => {
        const next = prev + 1;
        if (next >= run.sample_paths.length) {
          window.clearInterval(handle);
          return run.sample_paths.length;
        }
        return next;
      });
    }, 30);
    return () => window.clearInterval(handle);
  }, [run]);

  const chartData = React.useMemo(() => {
    if (!run) return [];
    const horizon = run.horizon_bars + 1;
    return Array.from({ length: horizon }, (_, i) => ({
      bar: i,
      p05: run.quantiles.p05[i],
      p25: run.quantiles.p25[i],
      p50: run.quantiles.p50[i],
      p75: run.quantiles.p75[i],
      p95: run.quantiles.p95[i],
    }));
  }, [run]);

  return (
    <Card>
      <CardHeader
        tag="3"
        title="Monte Carlo · pre-execution paths"
        trailing={
          <>
            {run?.blocked ? (
              <Chip tone="danger">
                <ShieldAlert className="h-3 w-3" />
                Blocked
              </Chip>
            ) : (
              <Chip tone="success">
                <ShieldCheck className="h-3 w-3" />
                Cleared
              </Chip>
            )}
            <Chip tone="info">{run ? `${run.n_paths.toLocaleString()} paths` : "—"}</Chip>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setAnimatedIdx(0);
              }}
            >
              Replay
            </Button>
          </>
        }
      />
      <CardBody>
        {!run && q.isLoading && <p className="text-text-3 text-[12px]">Loading…</p>}
        {!run && q.isError && (
          <p className="text-danger text-[12px]">Failed to fetch Monte Carlo run.</p>
        )}
        {run && (
          <>
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              <Stat label="Pair" value={run.pair} sub={run.side} />
              <Stat
                label="VaR 95%"
                value={fmtPct(run.var_95 * 100)}
                tone={run.var_95 < 0 ? "neg" : "default"}
              />
              <Stat
                label="ES 95%"
                value={fmtPct(run.expected_shortfall_95 * 100)}
                tone={run.expected_shortfall_95 < 0 ? "neg" : "default"}
              />
              <Stat label="Horizon" value={`${run.horizon_bars} bars`} />
            </div>

            <div className="mt-4 h-[280px] w-full">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={chartData} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="mc-p95" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.18} />
                      <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.02} />
                    </linearGradient>
                    <linearGradient id="mc-p75" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.32} />
                      <stop offset="100%" stopColor="var(--accent)" stopOpacity={0.06} />
                    </linearGradient>
                  </defs>
                  <XAxis
                    dataKey="bar"
                    tick={{ fill: "var(--text-3)", fontSize: 10 }}
                    stroke="var(--stroke-2)"
                  />
                  <YAxis
                    domain={["auto", "auto"]}
                    tick={{ fill: "var(--text-3)", fontSize: 10 }}
                    stroke="var(--stroke-2)"
                    tickFormatter={(v) => v.toFixed(3)}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "var(--bg-overlay)",
                      border: "1px solid var(--stroke-2)",
                      borderRadius: 6,
                      fontSize: 11,
                    }}
                    labelStyle={{ color: "var(--text-3)" }}
                  />
                  {/* p05-p95 outer envelope */}
                  <Area type="monotone" dataKey="p95" stroke="none" fill="url(#mc-p95)" />
                  <Area type="monotone" dataKey="p05" stroke="none" fill="var(--bg-page)" />
                  {/* p25-p75 inner band */}
                  <Area type="monotone" dataKey="p75" stroke="none" fill="url(#mc-p75)" />
                  <Area type="monotone" dataKey="p25" stroke="none" fill="var(--bg-page)" />

                  {/* Animated sample paths */}
                  {run.sample_paths.slice(0, animatedIdx).map((p, i) => (
                    <Line
                      key={i}
                      type="monotone"
                      data={p.values.map((v, bar) => ({ bar, v }))}
                      dataKey="v"
                      stroke="var(--text-4)"
                      strokeWidth={0.6}
                      strokeOpacity={0.35}
                      dot={false}
                      isAnimationActive={false}
                    />
                  ))}

                  {/* p50 median highlighted */}
                  <Line
                    type="monotone"
                    dataKey="p50"
                    stroke="var(--accent)"
                    strokeWidth={1.5}
                    dot={false}
                  />

                  <ReferenceLine y={1} stroke="var(--stroke-3)" strokeDasharray="2 4" />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </>
        )}
      </CardBody>
      <CardFooter className="flex items-center justify-between">
        {run?.blocked ? (
          <span className="text-danger">
            Veto · {run.block_reason || "risk threshold"} — trade was NOT executed.
          </span>
        ) : (
          <span>Path animation reveals {animatedIdx}/{run?.sample_paths.length ?? 0} sample trajectories.</span>
        )}
        <span className="num">Generated by quanta_core.risk.monte_carlo</span>
      </CardFooter>
    </Card>
  );
}
