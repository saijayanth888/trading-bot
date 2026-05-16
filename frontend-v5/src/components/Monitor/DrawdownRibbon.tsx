// owner: builder-C
// Monitor zone (scrolls). Current DD vs pause / kill thresholds.
// Builder D wires usePortfolio() → ddPct + thresholds; threshold crossings
// should trigger NumberRoll(trigger="dd-threshold-crossing") on the headline.
import { cn } from "@/lib/cn";
import { StaleChip, type StaleChipMeta } from "../StaleChip";
import { NumberRoll } from "../cells/NumberRoll";
import { FlashCell } from "../cells/FlashCell";

export interface DrawdownRibbonProps {
  ddPct?: number | null;
  pausePct?: number;
  killPct?: number;
  /** When true, headline uses NumberRoll because we crossed a threshold. */
  crossedThreshold?: boolean;
  meta?: StaleChipMeta | null;
  polling?: boolean;
}

export function DrawdownRibbon({
  ddPct = null,
  pausePct = 5,
  killPct = 10,
  crossedThreshold = false,
  meta,
  polling,
}: DrawdownRibbonProps) {
  const pct = ddPct ?? 0;
  const ratio = Math.min(1, Math.abs(pct) / killPct);
  const tone =
    Math.abs(pct) >= killPct
      ? "danger"
      : Math.abs(pct) >= pausePct
        ? "warn"
        : "info";

  const toneBg = {
    info: "bg-[color:var(--wong-blue)]",
    warn: "bg-[color:var(--wong-orange)]",
    danger: "bg-[color:var(--wong-vermillion)]",
  }[tone];

  return (
    <section
      aria-label="drawdown"
      className="rounded-lg border border-stroke-1 bg-bg-card p-4"
    >
      <header className="mb-3 flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider text-text-3">
          drawdown
        </h2>
        <StaleChip meta={meta} polling={polling} />
      </header>

      <div className="flex items-baseline gap-2">
        {crossedThreshold && ddPct != null ? (
          <NumberRoll
            value={ddPct}
            trigger="dd-threshold-crossing"
            className="text-2xl text-text-1"
            format={(v) => `${v.toFixed(2)}%`}
          />
        ) : (
          <FlashCell
            value={ddPct}
            className="text-2xl text-text-1"
            format={(v) =>
              typeof v === "number" ? `${v.toFixed(2)}%` : String(v)
            }
          />
        )}
        <span className="text-xs text-text-3 num">
          pause {pausePct}% · kill {killPct}%
        </span>
      </div>

      <div className="mt-3 h-2 w-full rounded-full bg-bg-inset overflow-hidden">
        <div
          className={cn("h-full transition-[width]", toneBg)}
          style={{ width: `${ratio * 100}%` }}
        />
      </div>
    </section>
  );
}
