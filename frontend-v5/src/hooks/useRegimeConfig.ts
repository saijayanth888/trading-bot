/**
 * Regime detector params — read + write. Replaces /ops card 19.
 * Mutation hits POST /api/v5/regime_config; legacy /api/ops/regime_config
 * proxies to v5 per spec §5.3 (mutating routes never 410).
 * owner: builder-D
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, endpoints } from "@/lib/api";
import type { RegimeConfigPayload } from "@/lib/types-fallback";

export function useRegimeConfig() {
  return useQuery<RegimeConfigPayload>({
    queryKey: ["v5", "regime_config"],
    queryFn: () => apiGet<RegimeConfigPayload>(endpoints.v5_regime_config),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}

export function useRegimeConfigMutation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<RegimeConfigPayload>) =>
      apiPost<RegimeConfigPayload>(endpoints.v5_regime_config, body),
    onSuccess: (data) => {
      qc.setQueryData(["v5", "regime_config"], data);
    },
  });
}
