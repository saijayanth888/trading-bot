/**
 * Number/date formatters — ported verbatim from `frontend-v4/src/lib/format.ts`
 * so v5 reads identically to the legacy SPA (operator visual continuity).
 *
 * Always return display-ready strings; never re-format the output of these
 * helpers. NB: P&L numerals retain muted green/red as SECONDARY signal per
 * spec §4.3 (Wong palette is PRIMARY for status/alerts).
 *
 * owner: builder-D
 */

export type SignedOpts = { decimals?: number; forceSign?: boolean };

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const MINUS = "−"; // U+2212, monospace-friendly

export function fmtMoney(v: number | null | undefined, opts: SignedOpts = {}): string {
  if (v == null || Number.isNaN(v)) return "—";
  const decimals = opts.decimals ?? 2;
  const sign = v < 0 ? MINUS : opts.forceSign && v > 0 ? "+" : "";
  const abs = Math.abs(v);
  const formatted =
    decimals === 2
      ? usd.format(abs)
      : abs.toLocaleString("en-US", {
          minimumFractionDigits: decimals,
          maximumFractionDigits: decimals,
          style: "currency",
          currency: "USD",
        });
  return sign + formatted;
}

export function fmtPct(v: number | null | undefined, opts: SignedOpts = {}): string {
  if (v == null || Number.isNaN(v)) return "—";
  const decimals = opts.decimals ?? 2;
  const sign = v < 0 ? MINUS : "+";
  return `${sign}${Math.abs(v).toFixed(decimals)}%`;
}

export function fmtPx(px: number | null | undefined): string {
  if (px == null || Number.isNaN(px)) return "—";
  const abs = Math.abs(px);
  if (abs >= 1000) return "$" + Math.round(px).toLocaleString("en-US");
  if (abs >= 1) return "$" + px.toFixed(2);
  if (abs >= 0.01) return "$" + px.toFixed(4);
  return "$" + px.toFixed(6);
}

export function fmtCompact(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + "k";
  return v.toFixed(0);
}

export function fmtLatencyMs(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

export function fmtAgo(iso: string | number | Date | null | undefined): string {
  if (iso == null || iso === "") return "—";
  const d = typeof iso === "object" ? iso : new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 0) return "just now";
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

/** Short staleness string used by `<StaleChip>`. */
export function fmtFeedAge(ageS: number | null | undefined): string {
  if (ageS == null || Number.isNaN(ageS)) return "—";
  if (ageS < 60) return `${Math.round(ageS)}s`;
  if (ageS < 3600) return `${Math.floor(ageS / 60)}m`;
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h`;
  return `${Math.floor(ageS / 86400)}d`;
}

export function fmtClockET(d: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

/** YYYY-MM-DD in UTC — used by `useNumberRollTrigger` for day rollover. */
export function utcDayKey(d: Date = new Date()): string {
  return d.toISOString().slice(0, 10);
}

/** Capital tier — integer count of $1k brackets crossed (floor). */
export function capitalTier(equityUsd: number | null | undefined): number {
  if (equityUsd == null || Number.isNaN(equityUsd)) return 0;
  return Math.floor(equityUsd / 1000);
}

export function classifyDelta(v: number | null | undefined): "pos" | "neg" | "flat" {
  if (v == null || Number.isNaN(v) || v === 0) return "flat";
  return v > 0 ? "pos" : "neg";
}

/** Sign + ▲/▼ arrow for P&L numerals — per spec §4.3 color-blind affordance. */
export function deltaGlyph(v: number | null | undefined): string {
  const cls = classifyDelta(v);
  if (cls === "pos") return "▲";
  if (cls === "neg") return "▼";
  return "·";
}
