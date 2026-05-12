import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Filter } from "lucide-react";
import { Card, CardHeader, CardBody, CardFooter } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { apiGet, endpoints } from "@/lib/api";
import type { ScreeningSnapshot, ScreeningName } from "@/types/v4";
import { fmtAgo } from "@/lib/format";
import { cn } from "@/lib/cn";

export function ScreeningGrid() {
  const q = useQuery({
    queryKey: ["v4", "screening"],
    queryFn: () => apiGet<ScreeningSnapshot>(endpoints.v4_screening),
    refetchInterval: 30_000,
  });

  const data = q.data;
  const names = data?.names ?? [];
  const traded = names.filter((n) => n.traded);
  const converged = names.filter((n) => n.converged);
  const detected = names.filter((n) => n.detected);

  return (
    <Card>
      <CardHeader
        tag="6"
        title="Universe · 27 names · 1–3 active"
        trailing={
          <>
            <Chip tone="info">
              <Filter className="h-3 w-3" />
              detected {detected.length}
            </Chip>
            <Chip tone="warn">converged {converged.length}</Chip>
            <Chip tone="success">trading {traded.length}</Chip>
          </>
        }
      />
      <CardBody>
        <TooltipProvider delayDuration={150}>
          <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-9">
            {names.map((n) => (
              <NameCell key={n.symbol} name={n} />
            ))}
            {names.length === 0 && (
              <div className="col-span-full rounded-[8px] border border-dashed border-stroke-1 p-6 text-center text-[12px] text-text-3">
                Screening snapshot not yet available.
              </div>
            )}
          </div>
        </TooltipProvider>

        {data && (
          <div className="mt-4 rounded-[8px] border border-stroke-1 bg-bg-card-2 p-3 text-[11px] num text-text-2">
            funnel · detected {data.funnel.detected} → converged {data.funnel.converged} → traded {data.funnel.traded}
            {data.funnel.detected > 0 && (
              <> · convergence rate {(((data.funnel.converged / data.funnel.detected) * 100) || 0).toFixed(0)}%</>
            )}
          </div>
        )}
      </CardBody>
      <CardFooter>
        Updated {fmtAgo(data?.generated_ts)} · 27 names = 12 crypto + 15 stocks (per universe.json)
      </CardFooter>
    </Card>
  );
}

function NameCell({ name }: { name: ScreeningName }) {
  const ring = name.traded
    ? "border-success bg-success-bg shadow-[inset_0_0_0_1px_var(--success-line)]"
    : name.converged
    ? "border-warn-line bg-warn-bg"
    : name.detected
    ? "border-info-line bg-info-bg"
    : "border-stroke-1 bg-bg-card-2";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className={cn("flex flex-col items-center gap-1 rounded-[8px] border px-2 py-2 transition-colors", ring)}>
          <span className="num text-[12px] font-medium">{name.symbol}</span>
          <span className="text-[9px] uppercase tracking-[0.10em] text-text-3">{name.asset_class}</span>
          {name.traded && <span className="h-1 w-6 rounded-full bg-success" />}
          {!name.traded && name.converged && <span className="h-1 w-6 rounded-full bg-warn" />}
          {!name.traded && !name.converged && name.detected && <span className="h-1 w-6 rounded-full bg-info" />}
          {!name.detected && <span className="h-1 w-6 rounded-full bg-text-4 opacity-40" />}
        </div>
      </TooltipTrigger>
      <TooltipContent>
        <div className="space-y-0.5">
          <div className="num text-[12px] font-medium">{name.symbol}</div>
          <div className="text-[11px] text-text-2">regime · {name.regime}</div>
          {name.thesis && <div className="text-[11px] text-text-2 max-w-[280px]">{name.thesis}</div>}
          {name.last_setup_ts && <div className="text-[11px] text-text-3">setup {fmtAgo(name.last_setup_ts)}</div>}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}
