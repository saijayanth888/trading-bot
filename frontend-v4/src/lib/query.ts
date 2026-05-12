import { QueryClient } from "@tanstack/react-query";

/**
 * One QueryClient for the whole app. SSE streams use raw EventSource (see
 * useDebateStream), TanStack only owns request/response endpoints.
 *
 * staleTime 15s · refetchInterval 30s mirrors the existing dashboard's
 * WS push cadence so V4 feels equally live without hammering the API.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchInterval: 30_000,
      refetchOnWindowFocus: true,
      retry: 1,
      gcTime: 5 * 60_000,
    },
    mutations: {
      retry: 0,
    },
  },
});
