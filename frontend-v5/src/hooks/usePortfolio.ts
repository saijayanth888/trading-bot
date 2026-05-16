/**
 * Capital / day-PnL / drawdown — fans out to <CapitalCard>, <DrawdownRibbon>.
 * Per spec §5.1 the payload carries `{combined, crypto, stocks}`.
 * owner: builder-D
 */
import { useQuery } from "@tanstack/react-query";
import { apiGet, endpoints } from "@/lib/api";
import type { PortfolioPayload } from "@/lib/types-fallback";

export function usePortfolio() {
  return useQuery<PortfolioPayload>({
    queryKey: ["v5", "portfolio"],
    queryFn: () => apiGet<PortfolioPayload>(endpoints.v5_portfolio),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
}
