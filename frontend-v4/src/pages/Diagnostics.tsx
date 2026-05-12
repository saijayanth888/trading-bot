import { SystemHealthStrip } from "@/components/quanta/SystemHealthStrip";
import { RegimeStrip } from "@/components/quanta/RegimeStrip";
import { PageHeader } from "@/pages/Overview";

export function DiagnosticsPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="Diagnostics" subtitle="Service probes · regime · live config" />
      <SystemHealthStrip />
      <RegimeStrip />
    </div>
  );
}
