/**
 * Priority detect feed. Sorted server-side by severity; UI may re-rank.
 * owner: builder-D
 */
import { useQuery } from "@tanstack/react-query";
import { apiGet, endpoints } from "@/lib/api";
import type { AlertsPayload } from "@/lib/types-fallback";

export function useAlerts() {
  return useQuery<AlertsPayload>({
    queryKey: ["v5", "alerts"],
    queryFn: () => apiGet<AlertsPayload>(endpoints.v5_alerts),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}
