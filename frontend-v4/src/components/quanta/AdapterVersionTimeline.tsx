import * as React from "react";
import {
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
  ReferenceLine,
} from "recharts";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { History, Undo2 } from "lucide-react";
import { apiGet, apiPost, endpoints } from "@/lib/api";
import { Card, CardHeader, CardBody, CardFooter } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { fmtAgo } from "@/lib/format";
import type { AdapterRecord, DebateRole } from "@/types/v4";

const ROLES: DebateRole[] = ["regime", "micro", "bull", "bear", "arbiter", "reflector"];

export function AdapterVersionTimeline() {
  const [role, setRole] = React.useState<DebateRole>("bull");
  const qc = useQueryClient();

  const adapters = useQuery({
    queryKey: ["v4", "adapters"],
    queryFn: () => apiGet<{ adapters: AdapterRecord[] }>(endpoints.v4_adapters),
    refetchInterval: 60_000,
  });

  const rollback = useMutation({
    mutationFn: (id: string) => apiPost(endpoints.v4_adapter_rollback(id)),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["v4", "adapters"] }),
  });

  const rows = (adapters.data?.adapters ?? []).filter((a) => a.role === role);

  return (
    <Card>
      <CardHeader
        tag="4"
        title="LoRA adapter promotion history"
        trailing={
          <>
            <Chip tone="info">
              <History className="h-3 w-3" />
              {rows.length} versions
            </Chip>
          </>
        }
      />
      <CardBody>
        <Tabs value={role} onValueChange={(v) => setRole(v as DebateRole)}>
          <TabsList>
            {ROLES.map((r) => (
              <TabsTrigger key={r} value={r}>
                {r}
              </TabsTrigger>
            ))}
          </TabsList>
          {ROLES.map((r) => (
            <TabsContent key={r} value={r} className="space-y-4">
              <ParetoChart rows={rows} />
              <TimelineRows rows={rows} onRollback={(id) => rollback.mutate(id)} pending={rollback.isPending} />
            </TabsContent>
          ))}
        </Tabs>
      </CardBody>
      <CardFooter>
        Source · ModelForge `/api/adapters` · promotion Sunday 14:00 ET
      </CardFooter>
    </Card>
  );
}

function ParetoChart({ rows }: { rows: AdapterRecord[] }) {
  const data = rows.map((a) => ({
    x: a.hit_rate,
    y: a.faithfulness,
    z: a.status === "champion" ? 220 : 90,
    id: a.id,
    status: a.status,
  }));

  return (
    <div className="rounded-[8px] border border-stroke-1 bg-bg-card-2 p-3">
      <div className="label mb-1">Pareto frontier · faithfulness × hit-rate</div>
      <div className="h-[200px]">
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <XAxis
              type="number"
              dataKey="x"
              domain={[0, 1]}
              tickFormatter={(v) => v.toFixed(2)}
              tick={{ fill: "var(--text-3)", fontSize: 10 }}
              stroke="var(--stroke-2)"
              label={{ value: "hit rate", position: "insideBottomRight", fill: "var(--text-3)", fontSize: 10 }}
            />
            <YAxis
              type="number"
              dataKey="y"
              domain={[0, 1]}
              tickFormatter={(v) => v.toFixed(2)}
              tick={{ fill: "var(--text-3)", fontSize: 10 }}
              stroke="var(--stroke-2)"
              label={{ value: "faithfulness", angle: -90, position: "insideLeft", fill: "var(--text-3)", fontSize: 10 }}
            />
            <ZAxis type="number" dataKey="z" range={[60, 220]} />
            <Tooltip
              contentStyle={{ background: "var(--bg-overlay)", border: "1px solid var(--stroke-2)", borderRadius: 6, fontSize: 11 }}
              formatter={(value: number, name: string) => [value.toFixed(3), name]}
              labelFormatter={() => ""}
            />
            <ReferenceLine y={0.5} stroke="var(--stroke-2)" strokeDasharray="2 4" />
            <ReferenceLine x={0.5} stroke="var(--stroke-2)" strokeDasharray="2 4" />
            <Scatter data={data} fill="var(--accent)" />
          </ScatterChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function TimelineRows({
  rows,
  onRollback,
  pending,
}: {
  rows: AdapterRecord[];
  onRollback: (id: string) => void;
  pending: boolean;
}) {
  if (rows.length === 0) {
    return (
      <div className="rounded-[8px] border border-dashed border-stroke-1 p-6 text-center text-[12px] text-text-3">
        No adapters promoted yet for this role.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {rows.map((a) => {
        const tone =
          a.status === "champion"
            ? "success"
            : a.status === "rolled_back"
            ? "danger"
            : a.status === "pareto"
            ? "info"
            : "warn";
        return (
          <div key={a.id} className="flex items-center gap-3 rounded-[8px] border border-stroke-1 bg-bg-card-2 px-3 py-2">
            <Chip tone={tone}>{a.status}</Chip>
            <div className="num text-[12px] font-medium">{a.id}</div>
            <div className="text-[11px] text-text-3">{a.base_model}</div>
            <div className="ml-auto flex items-center gap-3 text-[11px] num text-text-3">
              <span>fa·{a.faithfulness.toFixed(2)}</span>
              <span>hr·{a.hit_rate.toFixed(2)}</span>
              <span>{fmtAgo(a.promoted_at)}</span>
              {a.status !== "rolled_back" && a.status !== "champion" && (
                <Dialog>
                  <DialogTrigger asChild>
                    <Button size="sm" variant="ghost">
                      <Undo2 className="h-3.5 w-3.5" />
                      Rollback
                    </Button>
                  </DialogTrigger>
                  <DialogContent>
                    <DialogTitle>Roll back {a.id}?</DialogTitle>
                    <DialogDescription>
                      Calls <code className="num">POST /api/v4/adapters/{a.id}/rollback</code>. The current champion will be deactivated and the previous Pareto-frontier adapter will be reinstated. ModelForge restarts the affected Ollama Modelfile (~5s).
                    </DialogDescription>
                    <div className="mt-3 flex justify-end gap-2">
                      <Button variant="ghost" size="sm" onClick={() => void 0}>
                        Cancel
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        disabled={pending}
                        onClick={() => onRollback(a.id)}
                      >
                        Roll back
                      </Button>
                    </div>
                  </DialogContent>
                </Dialog>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
