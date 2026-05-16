/**
 * useFlashOnChange — returns `true` for 300ms whenever the dependency value
 * changes (per spec §4.3: 200-400ms accent flash on diff; we pick 300).
 *
 * This is the DEFAULT visual cue for routine ticks. NumberRoll is reserved
 * for three specific triggers handled by `useNumberRollTrigger`.
 *
 * owner: builder-D
 */
import { useEffect, useRef, useState } from "react";

export const FLASH_MS = 300;

export function useFlashOnChange<T>(value: T): boolean {
  const [flash, setFlash] = useState(false);
  const prev = useRef<T>(value);
  const initialised = useRef(false);

  useEffect(() => {
    // Don't flash on the very first render (no real "change" yet).
    if (!initialised.current) {
      initialised.current = true;
      prev.current = value;
      return;
    }
    if (!shallowEqual(prev.current, value)) {
      prev.current = value;
      setFlash(true);
      const t = setTimeout(() => setFlash(false), FLASH_MS);
      return () => clearTimeout(t);
    }
  }, [value]);

  return flash;
}

function shallowEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  // Numeric noise-free comparison for floats (1.000000001 ≈ 1).
  if (typeof a === "number" && typeof b === "number") {
    return Math.abs(a - b) < 1e-9;
  }
  return false;
}
