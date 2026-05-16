// owner: builder-C
// Shared className helper. Builder D — please leave this file untouched;
// any other lib/* file (api.ts, ws.ts, format.ts, types-fallback.ts) is yours.
import clsx, { type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
