// owner: builder-C
// Sticky top:0 banner — status pill + capital + day-P&L + WS/poll chip + clock
// + KILL access (mirrors fixed-dock KillAllButton for keyboard reach).
// Builder D wires:
//   - useStatus() → status/banner copy
//   - usePortfolio() → capital, day P&L
//   - useStreamHealth() → WS up/down, poll interval, latency
import { useEffect, useState } from "react";
import { cn } from "@/lib/cn";
import { StaleChip } from "./StaleChip";

type Status = "clear" | "watch" | "halt" | "unknown";

interface TopBarProps {
  // Builder D — wire these props or replace internals with your hooks.
  status?: Status;
  statusText?: string;
  capitalUsd?: number | null;
  dayPnlPct?: number | null;
  wsUp?: boolean;
  pollIntervalS?: number;
  onKill?: () => void;
}

function useClockET(): string {
  const [s, setS] = useState<string>(() => fmt());
  useEffect(() => {
    const t = setInterval(() => setS(fmt()), 1000);
    return () => clearInterval(t);
  }, []);
  return s;
}

function fmt(): string {
  try {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date()) + " ET";
  } catch {
    return new Date().toISOString().slice(11, 16) + " UTC";
  }
}

const statusToneMap: Record<Status, string> = {
  clear: "bg-[color:var(--status-clear)]/15 text-[color:var(--status-clear)] border-[color:var(--status-clear)]/30",
  watch: "bg-[color:var(--status-watch)]/15 text-[color:var(--status-watch)] border-[color:var(--status-watch)]/30",
  halt: "bg-[color:var(--status-halt)]/20 text-[color:var(--status-halt)] border-[color:var(--status-halt)]/40",
  unknown: "bg-bg-inset text-text-3 border-stroke-2",
};

export function TopBar({
  status = "unknown",
  statusText = "loading…",
  capitalUsd = null,
  dayPnlPct = null,
  wsUp,
  pollIntervalS = 10,
  onKill,
}: TopBarProps) {
  const clock = useClockET();
  const usingPolling = wsUp === false;

  return (
    <header
      className={cn(
        "sticky top-0 z-40",
        "bg-[color:var(--bg-overlay)] backdrop-blur",
        "border-b border-stroke-1",
      )}
    >
      <div className="mx-auto flex max-w-[1920px] items-center gap-4 px-6 py-2">
        {/* status pill */}
        <span
          className={cn(
            "inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium",
            statusToneMap[status],
          )}
          aria-live="polite"
        >
          <span
            className="h-2 w-2 rounded-full"
            style={{ backgroundColor: "currentColor" }}
          />
          {statusText}
        </span>

        {/* capital */}
        <span className="num text-sm text-text-1">
          {capitalUsd == null
            ? "—"
            : capitalUsd.toLocaleString("en-US", {
                style: "currency",
                currency: "USD",
                maximumFractionDigits: 2,
              })}
        </span>

        {/* day P&L (muted green/red with sign + arrow per §4.3) */}
        <span
          className={cn(
            "num text-sm",
            dayPnlPct == null
              ? "text-text-3"
              : dayPnlPct >= 0
                ? "text-[color:var(--pnl-up)]"
                : "text-[color:var(--pnl-down)]",
          )}
        >
          {dayPnlPct == null
            ? "—"
            : `${dayPnlPct >= 0 ? "▲" : "▼"} ${dayPnlPct >= 0 ? "+" : ""}${dayPnlPct.toFixed(2)}% day`}
        </span>

        <span className="flex-1" />

        {/* WS / poll chip — per frontend-debate G6 */}
        {wsUp === undefined ? (
          <StaleChip meta={null} />
        ) : usingPolling ? (
          <span className="inline-flex items-center gap-1 rounded border border-[color:var(--wong-orange)]/40 bg-[color:var(--wong-orange)]/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-[color:var(--wong-orange)] num">
            polling {pollIntervalS}s
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded border border-[color:var(--wong-blue)]/40 bg-[color:var(--wong-blue)]/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-[color:var(--wong-blue)] num">
            ws▰ live
          </span>
        )}

        {/* clock */}
        <span className="num text-xs text-text-2">{clock}</span>

        {/* KILL — top-right access (real dock is bottom-right fixed) */}
        <button
          type="button"
          onClick={onKill}
          className={cn(
            "rounded border border-[color:var(--status-halt)]/40",
            "bg-[color:var(--status-halt)]/10 px-3 py-1 text-xs font-semibold",
            "text-[color:var(--status-halt)] hover:bg-[color:var(--status-halt)]/20",
            "focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--status-halt)]",
          )}
        >
          ⛔ KILL
        </button>
      </div>
    </header>
  );
}
