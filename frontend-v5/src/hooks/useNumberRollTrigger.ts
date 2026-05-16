/**
 * useNumberRollTrigger — returns `true` for one frame ONLY when a spec §4.3
 * NumberRoll-eligible event fires:
 *
 *   1. `day-rollover`  : UTC date changed since last value (cumulative day P&L)
 *   2. `capital-tier`  : equity crossed a $1k tier (any direction)
 *   3. `dd-threshold`  : drawdown crossed pause (3%) or kill (10%) thresholds
 *
 * Everything else is a routine tick → use <FlashCell> instead. Bug-fix
 * Frontend audit 2026-05-14: "round-2 NumberRoll trigger surface" maps
 * here.
 *
 * owner: builder-D
 */
import { useEffect, useRef, useState } from "react";
import { capitalTier, utcDayKey } from "@/lib/format";

export type NumberRollKind = "day-rollover" | "capital-tier" | "dd-threshold";

const DD_PAUSE_PCT = 3;
const DD_KILL_PCT = 10;

export interface NumberRollOpts {
  /** Override default 3% pause threshold (e.g. per-strategy). */
  pausePct?: number;
  /** Override default 10% kill threshold. */
  killPct?: number;
}

export function useNumberRollTrigger(
  value: number | null | undefined,
  kind: NumberRollKind,
  opts: NumberRollOpts = {},
): boolean {
  const [triggered, setTriggered] = useState(false);
  const prevRef = useRef<{ value: number | null; dayKey: string } | null>(null);

  useEffect(() => {
    const now = new Date();
    const dayKey = utcDayKey(now);
    const v = typeof value === "number" && !Number.isNaN(value) ? value : null;

    const prev = prevRef.current;
    if (!prev) {
      prevRef.current = { value: v, dayKey };
      return; // no trigger on first observation
    }

    let fire = false;
    switch (kind) {
      case "day-rollover":
        fire = prev.dayKey !== dayKey;
        break;
      case "capital-tier":
        if (v != null && prev.value != null) {
          fire = capitalTier(prev.value) !== capitalTier(v);
        }
        break;
      case "dd-threshold": {
        if (v != null && prev.value != null) {
          const pause = opts.pausePct ?? DD_PAUSE_PCT;
          const kill = opts.killPct ?? DD_KILL_PCT;
          const crossed = (a: number, b: number, t: number) =>
            (a < t && b >= t) || (a >= t && b < t);
          fire =
            crossed(prev.value, v, pause) ||
            crossed(prev.value, v, kill);
        }
        break;
      }
    }

    prevRef.current = { value: v, dayKey };
    if (fire) {
      setTriggered(true);
      // single-shot — caller (NumberRoll component) consumes via key change
      const t = setTimeout(() => setTriggered(false), 50);
      return () => clearTimeout(t);
    }
  }, [value, kind, opts.pausePct, opts.killPct]);

  return triggered;
}
