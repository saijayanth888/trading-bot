// owner: builder-C
// Card-22 replacement — collapsed by default. B8 forensic surface (per spec
// §9 + bug-table). Builder D wires useDecisions(limit) → GET /api/v5/decisions.
import { CollapsibleCard } from "./_Collapsible";

export interface DecisionRow {
  id: string;
  ts: string;
  symbol: string;
  side: "buy" | "sell" | "flatten" | "skip";
  reason: string;
  pnlUsd?: number | null;
}

export interface DecisionAuditProps {
  decisions?: DecisionRow[];
}

export function DecisionAudit({ decisions = [] }: DecisionAuditProps) {
  return (
    <CollapsibleCard
      title="decision audit"
      subtitle="explainability — every fill"
    >
      {decisions.length === 0 ? (
        <div className="text-xs text-text-3">no decisions in window</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-xs">
            <thead>
              <tr className="text-text-3 text-left uppercase tracking-wider">
                <th className="py-1 pr-3">ts</th>
                <th className="py-1 pr-3">sym</th>
                <th className="py-1 pr-3">side</th>
                <th className="py-1 pr-3">reason</th>
                <th className="py-1 pr-3 text-right">pnl</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d) => (
                <tr key={d.id} className="border-t border-stroke-1">
                  <td className="num py-1 pr-3 text-text-3">{d.ts}</td>
                  <td className="num py-1 pr-3 text-text-1">{d.symbol}</td>
                  <td className="py-1 pr-3 text-text-2 uppercase">{d.side}</td>
                  <td className="py-1 pr-3 text-text-2">{d.reason}</td>
                  <td
                    className={`num py-1 pr-3 text-right ${
                      d.pnlUsd == null
                        ? "text-text-3"
                        : d.pnlUsd >= 0
                          ? "text-[color:var(--pnl-up)]"
                          : "text-[color:var(--pnl-down)]"
                    }`}
                  >
                    {d.pnlUsd == null ? "—" : d.pnlUsd.toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </CollapsibleCard>
  );
}
