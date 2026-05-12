/**
 * Number formatters preserved verbatim from app.js so V4 reads identical to
 * the legacy SPA. Always returns display-ready strings; never re-format the
 * output of these.
 */

export type SignedOpts = { decimals?: number; forceSign?: boolean };

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function fmtMoney(v: number | null | undefined, opts: SignedOpts = {}): string {
  if (v == null || Number.isNaN(v)) return "—";
  const decimals = opts.decimals ?? 2;
  const sign = v < 0 ? "−" : opts.forceSign && v > 0 ? "+" : "";
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
  const sign = v < 0 ? "−" : "+";
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
  if (!iso) return "—";
  const d = typeof iso === "object" ? iso : new Date(iso);
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

export function classifyDelta(v: number | null | undefined): "pos" | "neg" | "flat" {
  if (v == null || Number.isNaN(v) || v === 0) return "flat";
  return v > 0 ? "pos" : "neg";
}
