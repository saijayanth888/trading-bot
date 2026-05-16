/**
 * Single-truth Sharpe + max-DD + win-rate. Closes B3.
 * owner: builder-D
 */
import { useQuery } from "@tanstack/react-query";
import { apiGet, endpoints } from "@/lib/api";
import type { MetricsPayload } from "@/lib/types-fallback";

export function useMetrics() {
  return useQuery<MetricsPayload>({
    queryKey: ["v5", "metrics"],
    queryFn: () => apiGet<MetricsPayload>(endpoints.v5_metrics),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}
