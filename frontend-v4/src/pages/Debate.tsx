import { DebateTranscriptLive } from "@/components/quanta/DebateTranscriptLive";
import { PageHeader } from "@/pages/Overview";

export function DebatePage() {
  return (
    <div className="space-y-4">
      <PageHeader title="Debate floor" subtitle="30s deliberate · blind panel · hard-veto arbiter" />
      <DebateTranscriptLive />
    </div>
  );
}
