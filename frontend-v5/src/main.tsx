// owner: builder-C  (WS startup added by builder-D)
import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { startWs } from "./lib/ws";
import "./styles/globals.css";

// Single QueryClient. Per-component refetchInterval lives on the useQuery
// call (per spec §4.1 + frontend-debate rebuttal — solves "can't pause heavy
// cards" without abandoning one-page topology).
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 1_000,
    },
  },
});

// builder-D: start WS pipe so diffs land in the queryClient cache. When WS is
// down the hooks fall back to 10s refetchInterval (TopBar shows polling chip).
startWs(queryClient);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
