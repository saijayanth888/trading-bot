// owner: builder-C
// Severity ramp per spec §4.3: opacity 0.5 (info) → 0.8 (warn) → 1.0 (danger).
// Border-left color also encodes severity. Wong-anchored hues — NO red/green
// here (those are reserved for P&L numerals).
import { cn } from "@/lib/cn";
import { StaleChip, type StaleChipMeta } from "../StaleChip";

export type AlertSeverity = "info" | "warn" | "danger";
export type AlertKind =
  | "stale-feed"
  | "gate-breach"
  | "risk-violation"
  | "b2-class"
  | "other";

export interface AlertItemData {
  id: string;
  severity: AlertSeverity;
  kind: AlertKind;
  title: string;
  detail?: string;
  ts?: string;
  meta?: StaleChipMeta | null;
}

export interface AlertItemProps {
  alert: AlertItemData;
  onAck?: (id: string) => void;
}

const severityToneMap: Record<AlertSeverity, string> = {
  info:
    "border-l-[color:var(--sev-info-color)] bg-[color:var(--sev-info-color)]/[0.08] opacity-90",
  warn:
    "border-l-[color:var(--sev-warn-color)] bg-[color:var(--sev-warn-color)]/[0.12] opacity-95",
  danger:
    "border-l-[color:var(--sev-danger-color)] bg-[color:var(--sev-danger-color)]/[0.18]",
};

const severityLabelMap: Record<AlertSeverity, string> = {
  info: "info",
  warn: "warn",
  danger: "danger",
};

export function AlertItem({ alert, onAck }: AlertItemProps) {
  return (
    <div
      className={cn(
        "flex items-start gap-3 border-l-4 rounded-r px-3 py-2",
        "border-y border-r border-stroke-1",
        severityToneMap[alert.severity],
      )}
      role="listitem"
      data-kind={alert.kind}
    >
      <span className="num text-[10px] uppercase tracking-wider text-text-3 pt-0.5 w-14">
        {severityLabelMap[alert.severity]}
      </span>
      <div className="flex-1">
        <div className="text-sm text-text-1">{alert.title}</div>
        {alert.detail && (
          <div className="mt-0.5 text-xs text-text-3">{alert.detail}</div>
        )}
        <div className="mt-1 flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wider text-text-4">
            {alert.kind}
          </span>
          {alert.meta && <StaleChip meta={alert.meta} />}
          {alert.ts && (
            <span className="num text-[10px] text-text-4">{alert.ts}</span>
          )}
        </div>
      </div>
      {onAck && (
        <button
          type="button"
          onClick={() => onAck(alert.id)}
          className="text-[10px] uppercase tracking-wider text-text-3 hover:text-text-1"
        >
          ack
        </button>
      )}
    </div>
  );
}
