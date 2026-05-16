// owner: builder-C
// Priority feed sorted by severity. Builder D wires useAlerts() →
// AlertItemData[]. Empty state explicitly named (not a blank zone) so
// operator can distinguish "no alerts" from "feed broken".
import { AlertItem, type AlertItemData } from "./AlertItem";
import { StaleChip, type StaleChipMeta } from "../StaleChip";

export interface DetectFeedProps {
  alerts?: AlertItemData[];
  meta?: StaleChipMeta | null;
  polling?: boolean;
  onAck?: (id: string) => void;
}

const sevRank: Record<AlertItemData["severity"], number> = {
  danger: 0,
  warn: 1,
  info: 2,
};

export function DetectFeed({
  alerts = [],
  meta,
  polling,
  onAck,
}: DetectFeedProps) {
  const sorted = [...alerts].sort(
    (a, b) => sevRank[a.severity] - sevRank[b.severity],
  );

  return (
    <section
      aria-label="detect-feed"
      className="rounded-lg border border-stroke-1 bg-bg-card"
    >
      <header className="flex items-center justify-between border-b border-stroke-1 px-4 py-2">
        <h2 className="text-xs uppercase tracking-wider text-text-3">
          detect <span className="num">({sorted.length})</span>
        </h2>
        <StaleChip meta={meta} polling={polling} />
      </header>
      <div role="list" className="divide-y divide-stroke-1">
        {sorted.length === 0 ? (
          <div className="px-4 py-6 text-center text-xs text-text-3">
            all clear — no active alerts
          </div>
        ) : (
          sorted.map((a) => <AlertItem key={a.id} alert={a} onAck={onAck} />)
        )}
      </div>
    </section>
  );
}
