// owner: builder-C
// Shared leaf — every data-bearing card renders one of these (closes B14 by
// codification per spec §4.2 + frontend-debate G4). Reads producer `_meta`.
//
// Render rules:
//   fresh:                "feed: 12s"                 (text-3)
//   intentional freeze:   "feed: STALE 17h (NYSE closed)"  (text-3, no ⚠)
//   unintentional stale:  "feed: STALE 4m ⚠"          (warn / danger ramp)
//   WS-down poll fallback:"feed: 10s (polling)"       (warn, per G6)
import { cn } from "@/lib/cn";

export interface StaleChipMeta {
  age_s: number | null;
  stale: boolean;
  market_open_now?: boolean | null;
  snapshot_ts?: string | null;
}

export interface StaleChipProps {
  meta?: StaleChipMeta | null;
  /** When true, top-bar WS-down state forces "(polling)" suffix per G6. */
  polling?: boolean;
  /** Polling interval seconds, displayed when `polling`. */
  pollIntervalS?: number;
  className?: string;
}

function fmtAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

export function StaleChip({
  meta,
  polling = false,
  pollIntervalS = 10,
  className,
}: StaleChipProps) {
  if (!meta) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1 text-[10px] uppercase tracking-wide text-text-4",
          className,
        )}
      >
        feed: —
      </span>
    );
  }

  const age = meta.age_s ?? 0;
  const stale = meta.stale === true;
  const intentional = stale && meta.market_open_now === false;

  let tone: "fresh" | "intentional" | "warn" | "danger" = "fresh";
  if (intentional) tone = "intentional";
  else if (stale && age > 600) tone = "danger";
  else if (stale) tone = "warn";

  const toneClass = {
    fresh: "text-[color:var(--stale-fresh)]",
    intentional: "text-[color:var(--stale-intentional)]",
    warn: "text-[color:var(--stale-warn)]",
    danger: "text-[color:var(--stale-danger)]",
  }[tone];

  let label: string;
  if (polling) {
    label = `feed: ${pollIntervalS}s (polling)`;
  } else if (intentional) {
    label = `feed: STALE ${fmtAge(age)} (closed)`;
  } else if (stale) {
    label = `feed: STALE ${fmtAge(age)} ⚠`;
  } else {
    label = `feed: ${fmtAge(age)}`;
  }

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 text-[10px] uppercase tracking-wide num",
        toneClass,
        className,
      )}
      title={meta.snapshot_ts ?? undefined}
    >
      {label}
    </span>
  );
}
