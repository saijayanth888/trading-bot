/**
 * Unioned positions (crypto fills + wheel state + shark) — closes B6/B9.
 * owner: builder-D
 */
import { useQuery } from "@tanstack/react-query";
import { apiGet, endpoints } from "@/lib/api";
import type { PositionsPayload } from "@/lib/types-fallback";

export function usePositions() {
  return useQuery<PositionsPayload>({
    queryKey: ["v5", "positions"],
    queryFn: () => apiGet<PositionsPayload>(endpoints.v5_positions),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}
