import { HeroScoreboard } from "@/components/quanta/HeroScoreboard";
import { SystemHealthStrip } from "@/components/quanta/SystemHealthStrip";
import { RegimeStrip } from "@/components/quanta/RegimeStrip";
import { ScreeningGrid } from "@/components/quanta/ScreeningGrid";
import { DebateTranscriptLive } from "@/components/quanta/DebateTranscriptLive";

export function OverviewPage() {
  return (
    <div className="space-y-4">
      <PageHeader title="Overview" subtitle="Where is the money · what is the stack doing · why isn't anything trading" />
      <HeroScoreboard />
      <div className="grid gap-4 xl:grid-cols-[1.4fr,1fr]">
        <DebateTranscriptLive />
        <RegimeStrip />
      </div>
      <ScreeningGrid />
      <SystemHealthStrip />
    </div>
  );
}

export function PageHeader({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="flex items-end justify-between gap-3 pb-1">
      <div>
        <div className="label">Quanta V4 · operator console</div>
        <h1 className="text-[22px] font-semibold tracking-[-0.01em] text-text-1">{title}</h1>
      </div>
      {subtitle && <p className="hidden text-[12px] text-text-3 md:block">{subtitle}</p>}
    </div>
  );
}
