// owner: builder-C
// Hermes integration per spec §7. Builder D wires three sources:
//   - useHermesSchedule()  → /api/v5/hermes/schedule
//   - useHermesRuns(20)    → /api/v5/hermes/runs?limit=20
//   - useHermesHealth()    → /api/v5/hermes/health
// + onRetrigger(jobId) → POST /api/v5/actions/hermes/retrigger/{job}
import { StaleChip, type StaleChipMeta } from "../StaleChip";
import { cn } from "@/lib/cn";

export interface HermesJobRow {
  id: string;
  cron: string;
  nextFireTs?: string | null;
  lastStatus?: "ok" | "fail" | "running" | "unknown";
  lastRunTs?: string | null;
}

export interface HermesRunRow {
  id: string;
  jobId: string;
  startedTs: string;
  outcome: "ok" | "fail" | "running";
  tail?: string;
}

export interface HermesPanelProps {
  schedule?: HermesJobRow[];
  runs?: HermesRunRow[];
  healthOk?: boolean | null;
  meta?: StaleChipMeta | null;
  polling?: boolean;
  onRetrigger?: (jobId: string) => void;
  onViewRun?: (runId: string) => void;
}

const outcomeTone = {
  ok: "text-[color:var(--wong-blue)]",
  fail: "text-[color:var(--wong-vermillion)]",
  running: "text-[color:var(--wong-orange)]",
  unknown: "text-text-3",
};

export function HermesPanel({
  schedule = [],
  runs = [],
  healthOk = null,
  meta,
  polling,
  onRetrigger,
  onViewRun,
}: HermesPanelProps) {
  return (
    <section
      aria-label="hermes"
      className="rounded-lg border border-stroke-1 bg-bg-card"
    >
      <header className="flex items-center justify-between border-b border-stroke-1 px-4 py-2">
        <div className="flex items-center gap-3">
          <h2 className="text-xs uppercase tracking-wider text-text-3">
            hermes
          </h2>
          <span
            className={cn(
              "rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider",
              healthOk == null
                ? "bg-bg-inset text-text-3 border-stroke-2"
                : healthOk
                  ? "bg-[color:var(--wong-blue)]/15 text-[color:var(--wong-blue)] border-[color:var(--wong-blue)]/30"
                  : "bg-[color:var(--wong-vermillion)]/15 text-[color:var(--wong-vermillion)] border-[color:var(--wong-vermillion)]/30",
            )}
          >
            {healthOk == null ? "—" : healthOk ? "healthy" : "degraded"}
          </span>
        </div>
        <StaleChip meta={meta} polling={polling} />
      </header>

      <div className="grid grid-cols-2 gap-4 p-4">
        {/* schedule */}
        <div>
          <h3 className="mb-2 text-[10px] uppercase tracking-wider text-text-3">
            schedule
          </h3>
          <div className="space-y-1">
            {schedule.length === 0 ? (
              <div className="text-xs text-text-4">no scheduled jobs</div>
            ) : (
              schedule.map((j) => (
                <div
                  key={j.id}
                  className="flex items-center gap-2 rounded border border-stroke-1 px-2 py-1 text-xs"
                >
                  <span className="flex-1 truncate text-text-1">{j.id}</span>
                  <span className="num text-text-3">{j.cron}</span>
                  <span
                    className={cn(
                      "num",
                      outcomeTone[j.lastStatus ?? "unknown"],
                    )}
                  >
                    {j.lastStatus ?? "—"}
                  </span>
                  <button
                    type="button"
                    onClick={() => onRetrigger?.(j.id)}
                    disabled={!onRetrigger}
                    className="rounded border border-stroke-2 px-1.5 py-0.5 text-[10px] uppercase text-text-2 hover:border-[color:var(--wong-blue)]/40 hover:text-[color:var(--wong-blue)] disabled:opacity-40"
                  >
                    fire
                  </button>
                </div>
              ))
            )}
          </div>
        </div>

        {/* recent runs */}
        <div>
          <h3 className="mb-2 text-[10px] uppercase tracking-wider text-text-3">
            recent runs
          </h3>
          <div className="space-y-1">
            {runs.length === 0 ? (
              <div className="text-xs text-text-4">no runs in window</div>
            ) : (
              runs.map((r) => (
                <button
                  key={r.id}
                  type="button"
                  onClick={() => onViewRun?.(r.id)}
                  className="block w-full text-left rounded border border-stroke-1 px-2 py-1 text-xs hover:border-stroke-2"
                >
                  <div className="flex items-center gap-2">
                    <span className="flex-1 truncate text-text-1">
                      {r.jobId}
                    </span>
                    <span className={cn("num", outcomeTone[r.outcome])}>
                      {r.outcome}
                    </span>
                    <span className="num text-text-4">{r.startedTs}</span>
                  </div>
                  {r.tail && (
                    <div className="mt-0.5 truncate text-[10px] text-text-3">
                      {r.tail}
                    </div>
                  )}
                </button>
              ))
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
