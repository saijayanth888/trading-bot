import { BacktestParityDashboard } from "@/components/quanta/BacktestParityDashboard";
import { PageHeader } from "@/pages/Overview";

export function ParityPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="Backtest ↔ live parity" subtitle="14-day cutover gate · trade-for-trade reconciliation" />
      <BacktestParityDashboard />
    </div>
  );
}
