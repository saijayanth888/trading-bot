// owner: builder-C
// Monitor zone — scrolls, NOT sticky per spec §4.3 + frontend-debate G2.
// Builder D wires: usePortfolio() → equity / day P&L / sparkline series.
//
// Display rules per spec §4.3:
//   - Capital → NumberRoll on $1k tier-crossing only; else FlashCell.
//   - Day P&L → SECONDARY muted green/red on numerals + sign + arrow.
//   - Sparkline stroke uses Wong PRIMARY hue (blue), not muted greens.
import { LineChart, Line, ResponsiveContainer } from "recharts";
import { cn } from "@/lib/cn";
import { FlashCell } from "../cells/FlashCell";
import { StaleChip, type StaleChipMeta } from "../StaleChip";

export interface CapitalCardProps {
  equityUsd?: number | null;
  dayPnlUsd?: number | null;
  dayPnlPct?: number | null;
  sparkline?: Array<{ t: number; v: number }>;
  meta?: StaleChipMeta | null;
  polling?: boolean;
}

export function CapitalCard({
  equityUsd = null,
  dayPnlUsd = null,
  dayPnlPct = null,
  sparkline = [],
  meta,
  polling,
}: CapitalCardProps) {
  const dayUp = (dayPnlUsd ?? 0) >= 0;

  return (
    <section
      aria-label="capital"
      className="rounded-lg border border-stroke-1 bg-bg-card p-4"
    >
      <header className="mb-3 flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider text-text-3">
          capital
        </h2>
        <StaleChip meta={meta} polling={polling} />
      </header>

      <div className="flex items-baseline gap-3">
        <FlashCell
          value={equityUsd ?? null}
          className="text-3xl text-text-1"
          format={(v) =>
            typeof v === "number"
              ? v.toLocaleString("en-US", {
                  style: "currency",
                  currency: "USD",
                  maximumFractionDigits: 2,
                })
              : String(v)
          }
        />
        <span
          className={cn(
            "num text-sm",
            dayPnlUsd == null
              ? "text-text-3"
              : dayUp
                ? "text-[color:var(--pnl-up)]"
                : "text-[color:var(--pnl-down)]",
          )}
        >
          {dayPnlUsd == null ? (
            "—"
          ) : (
            <>
              {dayUp ? "▲" : "▼"} {dayUp ? "+" : ""}
              {dayPnlUsd.toFixed(2)}{" "}
              {dayPnlPct != null
                ? `(${dayUp ? "+" : ""}${dayPnlPct.toFixed(2)}%)`
                : null}
            </>
          )}
        </span>
      </div>

      <div className="mt-3 h-12">
        {sparkline.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={sparkline} margin={{ top: 2, right: 2, left: 2, bottom: 2 }}>
              <Line
                type="monotone"
                dataKey="v"
                stroke="var(--wong-blue)"
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-full w-full rounded bg-bg-inset" />
        )}
      </div>
    </section>
  );
}
