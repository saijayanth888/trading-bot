// owner: builder-C
// Two distinct instances per frontend-debate G5 + bug B12 — each bound to its
// own producer endpoint via Builder D:
//   <RegimeChip kind="crypto"> → /api/v5/strategies/crypto-v4.regime
//   <RegimeChip kind="stocks"> → /api/v5/strategies/stocks-wheel.regime
import { cn } from "@/lib/cn";
import { StaleChip, type StaleChipMeta } from "../StaleChip";

export type RegimeKind = "crypto" | "stocks";

export interface RegimeChipProps {
  kind: RegimeKind;
  regime?: string | null;
  confidence?: number | null;
  meta?: StaleChipMeta | null;
  polling?: boolean;
}

const kindLabel: Record<RegimeKind, string> = {
  crypto: "crypto-v4 regime",
  stocks: "stocks-wheel regime",
};

export function RegimeChip({
  kind,
  regime = null,
  confidence = null,
  meta,
  polling,
}: RegimeChipProps) {
  return (
    <section
      aria-label={`${kind}-regime`}
      className="rounded border border-stroke-1 bg-bg-card p-3"
      data-kind={kind}
    >
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wider text-text-3">
          {kindLabel[kind]}
        </span>
        <StaleChip meta={meta} polling={polling} />
      </div>
      <div className="mt-1 flex items-baseline gap-2">
        <span
          className={cn(
            "text-sm font-medium",
            regime ? "text-[color:var(--wong-blue)]" : "text-text-3",
          )}
        >
          {regime ?? "—"}
        </span>
        {confidence != null && (
          <span className="num text-[10px] text-text-3">
            conf {(confidence * 100).toFixed(0)}%
          </span>
        )}
      </div>
    </section>
  );
}
