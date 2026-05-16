/**
 * Decision audit — explainability for every fill (B8 forensic surface).
 * owner: builder-D
 */
import { useQuery } from "@tanstack/react-query";
import { apiGet, endpoints } from "@/lib/api";
import type { DecisionsPayload } from "@/lib/types-fallback";

export function useDecisions(limit = 50) {
  return useQuery<DecisionsPayload>({
    queryKey: ["v5", "decisions", limit],
    queryFn: () => apiGet<DecisionsPayload>(endpoints.v5_decisions(limit)),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}
