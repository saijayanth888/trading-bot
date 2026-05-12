import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, Pause, Play, RefreshCw } from "lucide-react";
import { Card, CardHeader, CardBody, CardFooter } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ScrollArea } from "@/components/ui/scroll-area";
import { apiGet, endpoints } from "@/lib/api";
import { fmtLatencyMs, fmtAgo } from "@/lib/format";
import { useDebate } from "@/store/debate";
import { useDebateStream } from "@/hooks/useDebateStream";
import type { AgentVote, DebateRole, DebateSession } from "@/types/v4";
import { cn } from "@/lib/cn";

const ROLE_META: Record<DebateRole, { label: string; tone: "info" | "success" | "danger" | "warn" | "accent"; stance: string }> = {
  regime: { label: "Regime", tone: "info", stance: "Macro context" },
  micro: { label: "Microstructure", tone: "info", stance: "Book health" },
  bull: { label: "Bull", tone: "success", stance: "Argue UP" },
  bear: { label: "Bear", tone: "danger", stance: "Argue DOWN" },
  arbiter: { label: "Arbiter", tone: "warn", stance: "Synthesise" },
  reflector: { label: "Reflector", tone: "accent", stance: "Post-mortem" },
};

const ROLES_ORDER: DebateRole[] = ["regime", "micro", "bull", "bear", "arbiter"];

interface HistoryItem {
  session_id: string;
  pair: string;
  setup_ts: string;
  decision?: string;
  total_latency_ms?: number;
}

interface DebateTranscriptLiveProps {
  initialSessionId?: string;
}

export function DebateTranscriptLive({ initialSessionId }: DebateTranscriptLiveProps) {
  const session = useDebate((s) => s.session);
  const status = useDebate((s) => s.status);
  const setSession = useDebate((s) => s.setSession);
  const reset = useDebate((s) => s.reset);
  const [sessionId, setSessionId] = React.useState<string | null>(initialSessionId ?? null);
  const [autoStream, setAutoStream] = React.useState(true);

  const history = useQuery({
    queryKey: ["debate", "history"],
    queryFn: () => apiGet<{ sessions: HistoryItem[] }>(endpoints.v4_debate_history),
    refetchInterval: 30_000,
  });

  // When a recent session lands and we have nothing selected, pick the newest.
  React.useEffect(() => {
    if (sessionId) return;
    const first = history.data?.sessions?.[0]?.session_id;
    if (first) setSessionId(first);
  }, [sessionId, history.data]);

  // Stream / reset when session id changes.
  React.useEffect(() => {
    reset();
    return reset;
  }, [sessionId, reset]);

  const { connected, connect, disconnect } = useDebateStream(autoStream ? sessionId : null, true);

  const sessions = history.data?.sessions ?? [];
  const current = session;

  return (
    <Card>
      <CardHeader
        tag="2"
        title="Debate transcript · 30s deliberate"
        trailing={
          <>
            <Chip tone={status === "live" ? "success" : status === "aborted" ? "danger" : "default"}>
              {status === "live" ? "LIVE" : status === "aborted" ? "ABORTED" : status === "complete" ? "COMPLETE" : "IDLE"}
            </Chip>
            <Select value={sessionId ?? ""} onValueChange={(v) => setSessionId(v || null)}>
              <SelectTrigger className="h-7 w-[220px]">
                <SelectValue placeholder="Select session…" />
              </SelectTrigger>
              <SelectContent>
                {sessions.map((s) => (
                  <SelectItem key={s.session_id} value={s.session_id}>
                    {s.pair} · {fmtAgo(s.setup_ts)} {s.decision ? `· ${s.decision}` : ""}
                  </SelectItem>
                ))}
                {sessions.length === 0 && <SelectItem value="__none">No sessions yet</SelectItem>}
              </SelectContent>
            </Select>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => (autoStream ? disconnect() : connect())}
              title={autoStream ? "Pause stream" : "Replay"}
            >
              {autoStream ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
            </Button>
            <Button
              size="icon"
              variant="ghost"
              onClick={() => {
                setSession(null);
                setAutoStream(true);
              }}
              title="Reset"
            >
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
          </>
        }
      />
      <CardBody className="grid gap-3 lg:grid-cols-2">
        {ROLES_ORDER.map((role) => (
          <DebateBubble
            key={role}
            role={role}
            vote={current?.votes.find((v) => v.role === role)}
          />
        ))}
        {current?.arbiter && (
          <div className="lg:col-span-2 rounded-[10px] border border-warn-line bg-warn-bg p-3">
            <div className="flex items-center gap-2">
              <Chip tone="warn">Arbiter synthesis</Chip>
              <span className="num text-[11px] text-text-2">
                {current.arbiter.agreement_pattern}
              </span>
            </div>
            <p className="mt-2 text-[12px] leading-relaxed text-text-1">
              {current.arbiter.synthesis_rationale}
            </p>
            {current.arbiter.dissent_notes.length > 0 && (
              <ul className="mt-2 list-disc pl-5 text-[11px] text-text-2 space-y-0.5">
                {current.arbiter.dissent_notes.map((note, i) => (
                  <li key={i}>{note}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        {current?.aggregate && (
          <DecisionPanel session={current} />
        )}
      </CardBody>
      <CardFooter className="flex items-center justify-between">
        <span>
          <Activity className="mr-1 inline h-3 w-3" />
          {connected ? "Stream open" : "Stream idle"}
        </span>
        <span className="num">
          {sessions.length} session{sessions.length === 1 ? "" : "s"} in window
        </span>
      </CardFooter>
    </Card>
  );
}

function DebateBubble({ role, vote }: { role: DebateRole; vote?: AgentVote }) {
  const partial = useDebate((s) => s.partials[role]);
  const meta = ROLE_META[role];
  const hasContent = Boolean(vote) || Boolean(partial);
  const tone = vote?.vote === "LONG" ? "success" : vote?.vote === "SHORT" ? "danger" : vote?.vote === "FLAT" ? "warn" : meta.tone;

  return (
    <div
      className={cn(
        "rounded-[10px] border bg-bg-card-2 p-3",
        vote ? "border-stroke-2" : "border-dashed border-stroke-1",
      )}
    >
      <div className="mb-2 flex items-center gap-2">
        <Chip tone={meta.tone}>{meta.label}</Chip>
        <span className="text-[10px] uppercase tracking-[0.10em] text-text-3">
          {meta.stance}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          {vote && (
            <Chip tone={tone}>{vote.vote} · {(vote.conviction * 100).toFixed(0)}%</Chip>
          )}
          {vote && <span className="num text-[10px] text-text-3">{fmtLatencyMs(vote.latency_ms)}</span>}
        </div>
      </div>

      <ScrollArea className="h-32">
        <div className="space-y-1 pr-2 text-[12px] leading-relaxed">
          {vote && vote.rationale && (
            <p className="animate-fade-in text-text-1 whitespace-pre-wrap">{vote.rationale}</p>
          )}
          {!vote && partial && (
            <p className="text-text-2 whitespace-pre-wrap">
              {partial}
              <span className="ml-0.5 inline-block h-3 w-1 animate-pulse bg-accent align-middle" />
            </p>
          )}
          {!hasContent && (
            <p className="text-text-4">Awaiting tokens…</p>
          )}
        </div>
      </ScrollArea>

      {vote?.evidence_keys && vote.evidence_keys.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {vote.evidence_keys.slice(0, 6).map((k) => (
            <span key={k} className="rounded-sm border border-stroke-1 bg-bg-inset px-1.5 py-0.5 text-[10px] num text-text-3">
              {k}
            </span>
          ))}
        </div>
      )}

      {vote && <div className="mt-2 text-[10px] num text-text-4">{vote.model} · {fmtAgo(vote.emitted_at)}</div>}
    </div>
  );
}

function DecisionPanel({ session }: { session: DebateSession }) {
  const decision = session.decision ?? "FLAT";
  const score = session.aggregate?.score ?? 0;
  const method = session.aggregate?.method ?? "—";
  const consensus = session.aggregate?.consensus ?? false;
  const tone = decision === "LONG" ? "success" : decision === "SHORT" ? "danger" : "warn";

  return (
    <div className="lg:col-span-2 rounded-[10px] border border-stroke-2 bg-bg-card p-4">
      <div className="flex items-center gap-3">
        <Chip tone={tone}>Decision · {decision}</Chip>
        <span className="num text-[12px] text-text-2">score {score.toFixed(3)}</span>
        <span className="num text-[12px] text-text-2">method · {method}</span>
        <span className="num text-[12px] text-text-2">
          consensus · {consensus ? "yes" : "no"}
        </span>
        <span className="ml-auto num text-[12px] text-text-3">
          {fmtLatencyMs(session.total_latency_ms)} total wall-time
        </span>
      </div>
    </div>
  );
}
