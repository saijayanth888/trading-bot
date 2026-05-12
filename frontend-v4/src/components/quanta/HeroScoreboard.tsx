import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, CardBody } from "@/components/ui/card";
import { Stat } from "@/components/ui/stat";
import { Chip } from "@/components/ui/chip";
import { Progress } from "@/components/ui/progress";
import { apiGet, endpoints } from "@/lib/api";
import { fmtMoney, fmtPct } from "@/lib/format";

interface Combined {
  combined: {
    equity: number;
    day_pnl: number;
    day_pct: number;
    crypto: { equity: number };
    stocks: { equity: number };
    breaker: string;
  };
  scoreboard?: {
    capital: number;
    live_pnl: number;
    realized_today: number;
    unrealized: number;
    drawdown: number;
    peak: number;
    open: { total: number; crypto: number; stocks: number };
    closed_today: number;
    pause_threshold: number;
    kill_threshold: number;
  };
}

export function HeroScoreboard() {
  const q = useQuery({
    queryKey: ["combined_portfolio"],
    queryFn: () => apiGet<Combined>(endpoints.combined_portfolio),
    refetchInterval: 15_000,
  });
  const data = q.data;
  const sb = data?.scoreboard;
  const dd = sb?.drawdown ?? 0;
  const pause = sb?.pause_threshold ?? 8;
  const kill = sb?.kill_threshold ?? 10;
  const fillPct = Math.min(100, (dd / kill) * 100);
  const tone = dd >= kill * 0.9 ? "danger" : dd >= pause ? "warn" : "success";

  return (
    <Card>
      <CardBody className="space-y-4">
        <div className="grid grid-cols-2 gap-5 md:grid-cols-5">
          <Stat
            label="Capital · combined"
            value={sb ? fmtMoney(sb.capital) : "—"}
            sub={sb ? `peak ${fmtMoney(sb.peak)}` : ""}
            large
          />
          <Stat
            label="Live P&L"
            value={sb ? fmtMoney(sb.live_pnl) : "—"}
            tone={(sb?.live_pnl ?? 0) >= 0 ? "pos" : "neg"}
          />
          <Stat
            label="Realized today"
            value={sb ? fmtMoney(sb.realized_today) : "—"}
            tone={(sb?.realized_today ?? 0) >= 0 ? "pos" : "neg"}
          />
          <Stat
            label="Unrealized"
            value={sb ? fmtMoney(sb.unrealized) : "—"}
            tone={(sb?.unrealized ?? 0) >= 0 ? "pos" : "neg"}
          />
          <Stat
            label="Drawdown"
            value={fmtPct(-dd)}
            sub={`pause ${pause}% · kill ${kill}%`}
            tone={tone === "danger" ? "neg" : tone === "warn" ? "warn" : "default"}
          />
        </div>

        <div className="space-y-2">
          <div className="flex items-center gap-2 text-[11px] text-text-3">
            <Chip tone={tone === "danger" ? "danger" : tone === "warn" ? "warn" : "success"}>
              DD ribbon
            </Chip>
            <span className="num">
              {sb ? `${fmtPct(-dd)} of ${kill}% kill threshold` : "—"}
            </span>
          </div>
          <Progress value={fillPct} tone={tone === "danger" ? "danger" : tone === "warn" ? "warn" : "success"} />
        </div>

        {sb && (
          <div className="flex flex-wrap gap-2 text-[11px] num text-text-3">
            <Chip tone="info">{sb.open.total} open</Chip>
            <Chip tone="info">{sb.open.crypto} crypto</Chip>
            <Chip tone="info">{sb.open.stocks} stocks</Chip>
            <Chip tone="default">{sb.closed_today} closed today</Chip>
            <span className="ml-auto">breaker · {data?.combined.breaker ?? "—"}</span>
          </div>
        )}
      </CardBody>
    </Card>
  );
}
