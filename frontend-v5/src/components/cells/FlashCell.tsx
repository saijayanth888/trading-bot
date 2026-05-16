// owner: builder-C
// Default cell for routine numeric ticks — 200-400ms accent flash on diff
// per spec §4.3 (Flash-on-change, NOT NumberRoll). NumberRoll is restricted
// to three specific triggers (see NumberRoll.tsx) per frontend-debate G3.
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/cn";

export interface FlashCellProps {
  value: number | string | null | undefined;
  /** When `value` is numeric, color the flash up/down on increase/decrease. */
  directional?: boolean;
  className?: string;
  /** Optional display formatter (defaults to String()). */
  format?: (v: number | string) => string;
}

export function FlashCell({
  value,
  directional = false,
  className,
  format,
}: FlashCellProps) {
  const prev = useRef<number | string | null | undefined>(value);
  const [tone, setTone] = useState<"none" | "accent" | "up" | "down">("none");

  useEffect(() => {
    if (value === undefined || value === null) {
      prev.current = value;
      return;
    }
    if (prev.current === value) return;
    if (
      directional &&
      typeof value === "number" &&
      typeof prev.current === "number"
    ) {
      setTone(value >= prev.current ? "up" : "down");
    } else {
      setTone("accent");
    }
    prev.current = value;
    const t = setTimeout(() => setTone("none"), 320);
    return () => clearTimeout(t);
  }, [value, directional]);

  const animClass = {
    none: "",
    accent: "animate-flash-accent",
    up: "animate-flash-up",
    down: "animate-flash-down",
  }[tone];

  const display =
    value === null || value === undefined
      ? "—"
      : format
        ? format(value)
        : String(value);

  return (
    <span className={cn("num inline-block rounded px-1", animClass, className)}>
      {display}
    </span>
  );
}
