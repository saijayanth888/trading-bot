import { ScreeningGrid } from "@/components/quanta/ScreeningGrid";
import { PageHeader } from "@/pages/Overview";

export function ScreeningPage() {
  return (
    <div className="space-y-4">
      <PageHeader
        title="Universe · 27 names"
        subtitle="12 crypto + 15 stocks · 1-3 traded per week · convergence funnel"
      />
      <ScreeningGrid />
    </div>
  );
}
