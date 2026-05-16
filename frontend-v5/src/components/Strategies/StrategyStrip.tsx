// owner: builder-C
// Per-strategy strip. Builder D wires useStrategy(kind) → equity/positions/
// regime/win-rate/sharpe (all sourced from `producers.metrics` per spec §5.1
// single-truth — closes B2/B3).
//
// Routine value ticks use FlashCell (per frontend-debate G3). NumberRoll is
// NOT used on this strip — strategies are routine cadence.
import { cn } from "@/lib/cn";
import { FlashCell } from "../cells/FlashCell";
import { StaleChip, type StaleChipMeta } from "../StaleChip";

export type StrategyKind = "crypto-v4" | "stocks-wheel" | "shark";

export interface StrategyStripProps {
  kind: StrategyKind;
  equityUsd?: number | null;
  dayPnlPct?: number | null;
  openPositions?: number | null;
  sharpe?: number | null;
  winRatePct?: number | null;
  regime?: string | null;
  status?: "running" | "paused" | "halted" | "unknown";
  meta?: StaleChipMeta | null;
  polling?: boolean;
  onPause?: (kind: StrategyKind) => void;
}

const kindLabel: Record<StrategyKind, string> = {
  "crypto-v4": "crypto-v4",
  "stocks-wheel": "stocks-wheel",
  shark: "shark",
};

const statusToneMap = {
  running:
    "bg-[color:var(--wong-blue)]/15 text-[color:var(--wong-blue)] border-[color:var(--wong-blue)]/30",
  paused:
    "bg-[color:var(--wong-orange)]/15 text-[color:var(--wong-orange)] border-[color:var(--wong-orange)]/30",
  halted:
    "bg-[color:var(--wong-vermillion)]/15 text-[color:var(--wong-vermillion)] border-[color:var(--wong-vermillion)]/30",
  unknown: "bg-bg-inset text-text-3 border-stroke-2",
};

export function StrategyStrip({
  kind,
  equityUsd = null,
  dayPnlPct = null,
  openPositions = null,
  sharpe = null,
  winRatePct = null,
  regime = null,
  status = "unknown",
  meta,
  polling,
  onPause,
}: StrategyStripProps) {
  const dayUp = (dayPnlPct ?? 0) >= 0;
  return (
    <section
      aria-label={`strategy-${kind}`}
      className="flex items-center gap-4 rounded-lg border border-stroke-1 bg-bg-card px-4 py-2"
    >
      <span className="w-32 text-xs uppercase tracking-wider text-text-2">
        {kindLabel[kind]}
      </span>

      <span
        className={cn(
          "rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider",
          statusToneMap[status],
        )}
      >
        {status}
      </span>

      <div className="flex items-baseline gap-1">
        <span className="text-[10px] uppercase text-text-3">equity</span>
        <FlashCell
          value={equityUsd}
          className="text-sm text-text-1"
          format={(v) =>
            typeof v === "number"
              ? v.toLocaleString("en-US", {
                  style: "currency",
                  currency: "USD",
                  maximumFractionDigits: 0,
                })
              : String(v)
          }
        />
      </div>

      <div className="flex items-baseline gap-1">
        <span className="text-[10px] uppercase text-text-3">day</span>
        <span
          className={cn(
            "num text-sm",
            dayPnlPct == null
              ? "text-text-3"
              : dayUp
                ? "text-[color:var(--pnl-up)]"
                : "text-[color:var(--pnl-down)]",
          )}
        >
          {dayPnlPct == null
            ? "—"
            : `${dayUp ? "▲" : "▼"} ${dayUp ? "+" : ""}${dayPnlPct.toFixed(2)}%`}
        </span>
      </div>

      <div className="flex items-baseline gap-1">
        <span className="text-[10px] uppercase text-text-3">pos</span>
        <FlashCell value={openPositions} className="text-sm text-text-1" />
      </div>

      <div className="flex items-baseline gap-1">
        <span className="text-[10px] uppercase text-text-3">sharpe</span>
        <FlashCell
          value={sharpe}
          className="text-sm text-text-1"
          format={(v) => (typeof v === "number" ? v.toFixed(2) : String(v))}
        />
      </div>

      <div className="flex items-baseline gap-1">
        <span className="text-[10px] uppercase text-text-3">win</span>
        <FlashCell
          value={winRatePct}
          className="text-sm text-text-1"
          format={(v) => (typeof v === "number" ? `${v.toFixed(0)}%` : String(v))}
        />
      </div>

      <div className="flex items-baseline gap-1">
        <span className="text-[10px] uppercase text-text-3">regime</span>
        <span className="text-sm text-[color:var(--wong-blue)]">
          {regime ?? "—"}
        </span>
      </div>

      <span className="flex-1" />

      <StaleChip meta={meta} polling={polling} />

      <button
        type="button"
        disabled={!onPause || status === "halted"}
        onClick={() => onPause?.(kind)}
        className={cn(
          "rounded border border-stroke-2 px-2 py-1 text-[10px] uppercase tracking-wider text-text-2",
          "hover:border-[color:var(--wong-orange)]/40 hover:text-[color:var(--wong-orange)]",
          "disabled:opacity-40 disabled:cursor-not-allowed",
        )}
      >
        ⏸ pause
      </button>
    </section>
  );
}
