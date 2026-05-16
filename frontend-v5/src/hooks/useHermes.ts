/**
 * Hermes: cron schedule, run history, composite health, retrigger mutation.
 * owner: builder-D
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, endpoints } from "@/lib/api";
import type {
  HermesHealthPayload,
  HermesRunsPayload,
  HermesSchedulePayload,
} from "@/lib/types-fallback";

export function useHermesSchedule() {
  return useQuery<HermesSchedulePayload>({
    queryKey: ["v5", "hermes", "schedule"],
    queryFn: () => apiGet<HermesSchedulePayload>(endpoints.v5_hermes_schedule),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}

export function useHermesRuns(limit = 20) {
  return useQuery<HermesRunsPayload>({
    queryKey: ["v5", "hermes", "runs", limit],
    queryFn: () => apiGet<HermesRunsPayload>(endpoints.v5_hermes_runs(limit)),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}

export function useHermesHealth() {
  return useQuery<HermesHealthPayload>({
    queryKey: ["v5", "hermes", "health"],
    queryFn: () => apiGet<HermesHealthPayload>(endpoints.v5_hermes_health),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}

/** Combined accessor for <HermesPanel>. */
export function useHermes() {
  const schedule = useHermesSchedule();
  const runs = useHermesRuns(20);
  const health = useHermesHealth();
  return { schedule, runs, health };
}

export function useHermesRetrigger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => apiPost(endpoints.v5_action_hermes_retrigger(jobId)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["v5", "hermes"] });
    },
  });
}
