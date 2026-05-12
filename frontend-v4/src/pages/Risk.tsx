import * as React from "react";
import { useSearchParams } from "react-router-dom";
import { MonteCarloPathViewer } from "@/components/quanta/MonteCarloPathViewer";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { PageHeader } from "@/pages/Overview";

export function RiskPage() {
  const [params, setParams] = useSearchParams();
  const tradeId = params.get("trade") ?? "preview";
  const [draft, setDraft] = React.useState(tradeId);

  return (
    <div className="space-y-4">
      <PageHeader title="Risk · Monte Carlo" subtitle="10k path pre-execution distribution · VaR/ES gate" />
      <Card>
        <CardHeader tag="3a" title="Trade selector" />
        <CardBody className="flex items-center gap-3">
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="trade_id (or 'preview')"
            className="h-8 w-72 rounded-[6px] border border-stroke-2 bg-bg-card-2 px-2 text-[12px] text-text-1 num focus-ring"
          />
          <button
            className="h-8 rounded-[6px] border border-accent-line bg-accent-bg px-3 text-[11px] uppercase tracking-[0.08em] text-accent focus-ring"
            onClick={() => setParams({ trade: draft })}
          >
            Load
          </button>
          <span className="text-[11px] text-text-3">
            Pass <code className="num">?trade=&lt;id&gt;</code> in the URL to deep-link a specific run.
          </span>
        </CardBody>
      </Card>
      <MonteCarloPathViewer tradeId={tradeId} />
    </div>
  );
}
