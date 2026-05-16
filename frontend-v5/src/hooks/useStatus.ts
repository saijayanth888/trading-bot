/**
 * Aggregate operator state (TopBar banner + detect counts).
 * owner: builder-D
 */
import { useQuery } from "@tanstack/react-query";
import { apiGet, endpoints } from "@/lib/api";
import type { StatusPayload } from "@/lib/types-fallback";

export function useStatus() {
  return useQuery<StatusPayload>({
    queryKey: ["v5", "status"],
    queryFn: () => apiGet<StatusPayload>(endpoints.v5_status),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}
