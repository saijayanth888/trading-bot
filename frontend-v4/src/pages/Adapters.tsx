import { AdapterVersionTimeline } from "@/components/quanta/AdapterVersionTimeline";
import { PageHeader } from "@/pages/Overview";

export function AdaptersPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="Adapters · LoRA registry" subtitle="Weekly Sunday 14:00 ET promotion · Pareto frontier" />
      <AdapterVersionTimeline />
    </div>
  );
}
