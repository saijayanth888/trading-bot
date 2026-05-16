// owner: builder-C
// NumberRoll — RESTRICTED to three triggers per frontend-debate G3 + spec §4.3:
//   (a) day-rollover of cumulative day P&L
//   (b) capital crossing a $1k tier
//   (c) DD crossing pause or kill threshold
// Routine ticks must use FlashCell, NOT this.
//
// The `trigger` prop is REQUIRED and exists primarily to make misuse loud at
// the type level — every call site must declare the rollover reason.
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

export type NumberRollTrigger =
  | "day-rollover"
  | "capital-tier-crossing"
  | "dd-threshold-crossing";

export interface NumberRollProps {
  value: number;
  trigger: NumberRollTrigger;
  className?: string;
  format?: (v: number) => string;
  /** Roll duration in ms (default 600). */
  durationMs?: number;
}

export function NumberRoll({
  value,
  trigger,
  className,
  format,
  durationMs = 600,
}: NumberRollProps) {
  const prev = useRef(value);
  const [display, setDisplay] = useState(value);

  useEffect(() => {
    if (prev.current === value) return;
    const start = performance.now();
    const from = prev.current;
    const to = value;
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      // ease-out cubic
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplay(from + (to - from) * eased);
      if (t < 1) raf = requestAnimationFrame(tick);
      else prev.current = to;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, durationMs]);

  return (
    <span
      className={cn("num inline-block tabular-nums", className)}
      data-numberroll-trigger={trigger}
    >
      {format ? format(display) : display.toFixed(2)}
    </span>
  );
}
