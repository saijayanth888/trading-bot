import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CheckCircle2, AlertTriangle } from "lucide-react";
import { Card, CardHeader, CardBody, CardFooter } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Stat } from "@/components/ui/stat";
import { Progress } from "@/components/ui/progress";
import { apiGet, endpoints } from "@/lib/api";
import type { ParitySummary } from "@/types/v4";
import { fmtPct } from "@/lib/format";

export function BacktestParityDashboard() {
  const q = useQuery({
    queryKey: ["v4", "parity"],
    queryFn: () => apiGet<ParitySummary>(endpoints.v4_parity),
    refetchInterval: 60_000,
  });
  const data = q.data;

  const consecutive = data?.consecutive_days_ok ?? 0;
  const threshold = data?.cutover_threshold_days ?? 14;
  const remaining = Math.max(0, threshold - consecutive);
  const progress = Math.min(100, (consecutive / threshold) * 100);

  const divergentRows = (data?.rows ?? []).filter((r) => r.divergent);

  return (
    <Card>
      <CardHeader
        tag="5"
        title="Backtest ↔ live parity"
        trailing={
          remaining === 0 ? (
            <Chip tone="success">
              <CheckCircle2 className="h-3 w-3" />
              Gate cleared
            </Chip>
          ) : (
            <Chip tone="warn">
              <AlertTriangle className="h-3 w-3" />
              T-{remaining}d to cutover
            </Chip>
          )
        }
      />
      <CardBody className="space-y-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat
            label="Consecutive OK days"
            value={`${consecutive}/${threshold}`}
            tone={consecutive >= threshold ? "pos" : "default"}
          />
          <Stat label="Divergent rows" value={divergentRows.length.toString()} tone={divergentRows.length === 0 ? "pos" : "warn"} />
          <Stat label="Rows in window" value={(data?.rows.length ?? 0).toString()} />
          <Stat label="Weeks tracked" value={(data?.weeks.length ?? 0).toString()} />
        </div>

        <div>
          <div className="label mb-1">Cutover gate · DG-2</div>
          <Progress value={progress} tone={consecutive >= threshold ? "success" : "warn"} />
        </div>

        <div className="rounded-[8px] border border-stroke-1 bg-bg-card-2 p-3">
          <div className="label mb-2">Per-week divergence %</div>
          <div className="h-[180px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={data?.weeks ?? []} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid stroke="var(--grid)" strokeDasharray="2 4" vertical={false} />
                <XAxis dataKey="iso" tick={{ fill: "var(--text-3)", fontSize: 10 }} stroke="var(--stroke-2)" />
                <YAxis
                  tick={{ fill: "var(--text-3)", fontSize: 10 }}
                  stroke="var(--stroke-2)"
                  tickFormatter={(v) => `${v}%`}
                />
                <Tooltip
                  contentStyle={{ background: "var(--bg-overlay)", border: "1px solid var(--stroke-2)", borderRadius: 6, fontSize: 11 }}
                  formatter={(v: number) => [`${v.toFixed(2)}%`, "divergence"]}
                />
                <Bar dataKey="divergence_pct" radius={[2, 2, 0, 0]}>
                  {(data?.weeks ?? []).map((w, i) => (
                    <Cell key={i} fill={w.divergence_pct > 10 ? "var(--danger)" : w.divergence_pct > 5 ? "var(--warn)" : "var(--success)"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="rounded-[8px] border border-stroke-1 bg-bg-card-2">
          <div className="flex items-center justify-between border-b border-stroke-1 px-3 py-2">
            <span className="label">Trade-for-trade reconciliation (most recent 12)</span>
            <span className="num text-[11px] text-text-3">
              {data?.rows.length ?? 0} rows
            </span>
          </div>
          <div className="divide-y divide-stroke-1">
            {(data?.rows ?? []).slice(0, 12).map((r) => (
              <div key={r.ts + r.pair} className="grid grid-cols-[140px,80px,1fr,1fr,80px,60px] gap-3 px-3 py-1.5 text-[11px]">
                <span className="num text-text-3">{r.ts}</span>
                <span className="num font-medium text-text-1">{r.pair}</span>
                <span className="num">live · {r.live_action}{r.live_pnl != null ? ` (${fmtPct(r.live_pnl * 100)})` : ""}</span>
                <span className="num">bt · {r.backtest_action}{r.backtest_pnl != null ? ` (${fmtPct(r.backtest_pnl * 100)})` : ""}</span>
                <span className="num text-text-3">
                  Δ{r.live_pnl != null && r.backtest_pnl != null ? fmtPct((r.live_pnl - r.backtest_pnl) * 100) : "—"}
                </span>
                <span>{r.divergent ? <Chip tone="danger">DIV</Chip> : <Chip tone="success">OK</Chip>}</span>
              </div>
            ))}
            {data && data.rows.length === 0 && (
              <div className="px-3 py-4 text-center text-[12px] text-text-3">No parity rows in the active window.</div>
            )}
          </div>
        </div>
      </CardBody>
      <CardFooter>
        DG-2 gate: 14 consecutive days under 10% divergence — required before PROMOTE V4.
      </CardFooter>
    </Card>
  );
}
