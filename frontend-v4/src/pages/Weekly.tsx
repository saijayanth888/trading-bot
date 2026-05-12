import { WeeklyPreviewLive } from "@/components/quanta/WeeklyPreviewLive";
import { PageHeader } from "@/pages/Overview";

export function WeeklyPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="Weekly publisher · preview" subtitle="What WOULD publish if Friday were now" />
      <WeeklyPreviewLive />
    </div>
  );
}
