/**
 * Per-strategy strip. Each strategy is its own query so cards can refetch
 * independently and the per-side `regime` field stays scoped (closes B12).
 * owner: builder-D
 */
import { useQuery, useQueries } from "@tanstack/react-query";
import { apiGet, endpoints } from "@/lib/api";
import type { StrategyKind, StrategyPayload } from "@/lib/types-fallback";

export function useStrategy(kind: StrategyKind) {
  return useQuery<StrategyPayload>({
    queryKey: ["v5", "strategies", kind],
    queryFn: () => apiGet<StrategyPayload>(endpoints.v5_strategy(kind)),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}

const ALL_KINDS: StrategyKind[] = ["crypto-v4", "stocks-wheel", "shark"];

export function useStrategies() {
  const queries = useQueries({
    queries: ALL_KINDS.map((kind) => ({
      queryKey: ["v5", "strategies", kind],
      queryFn: () => apiGet<StrategyPayload>(endpoints.v5_strategy(kind)),
      refetchInterval: 10_000,
      staleTime: 5_000,
    })),
  });
  return {
    crypto: queries[0],
    stocks: queries[1],
    shark: queries[2],
    isLoading: queries.some((q) => q.isLoading),
    isError: queries.some((q) => q.isError),
  };
}
