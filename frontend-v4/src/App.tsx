import * as React from "react";
import { Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { TopBar } from "@/components/layout/TopBar";
import { Sidebar } from "@/components/layout/Sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { OverviewPage } from "@/pages/Overview";
import { DebatePage } from "@/pages/Debate";
import { RiskPage } from "@/pages/Risk";
import { AdaptersPage } from "@/pages/Adapters";
import { ParityPage } from "@/pages/Parity";
import { ScreeningPage } from "@/pages/Screening";
import { WeeklyPage } from "@/pages/Weekly";
import { DiagnosticsPage } from "@/pages/Diagnostics";
import { NotFoundPage } from "@/pages/NotFound";
import { apiGet, endpoints } from "@/lib/api";
import { useUi } from "@/store/ui";

interface CombinedHeader {
  combined?: { equity: number; day_pct: number };
}

export default function App() {
  const theme = useUi((s) => s.theme);

  React.useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const q = useQuery({
    queryKey: ["combined_portfolio_header"],
    queryFn: () => apiGet<CombinedHeader>(endpoints.combined_portfolio),
    refetchInterval: 30_000,
  });

  return (
    <TooltipProvider delayDuration={150}>
      <div className="min-h-screen bg-bg-page text-text-1">
        <TopBar
          combinedEquity={q.data?.combined?.equity}
          dayPct={q.data?.combined?.day_pct}
        />
        <div className="mx-auto flex w-full max-w-[1500px] gap-0">
          <Sidebar />
          <main className="min-w-0 flex-1 px-5 py-5 md:px-6">
            <Routes>
              <Route path="/" element={<OverviewPage />} />
              <Route path="/debate" element={<DebatePage />} />
              <Route path="/risk" element={<RiskPage />} />
              <Route path="/adapters" element={<AdaptersPage />} />
              <Route path="/parity" element={<ParityPage />} />
              <Route path="/screening" element={<ScreeningPage />} />
              <Route path="/weekly" element={<WeeklyPage />} />
              <Route path="/diagnostics" element={<DiagnosticsPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Routes>
          </main>
        </div>
        <footer className="mx-auto mt-8 max-w-[1500px] px-5 pb-6 text-[10px] uppercase tracking-[0.10em] text-text-4">
          quanta v4 · feat/v4-wave2-frontend · greenfield React 19 + shadcn + Tailwind 4 + Geist
        </footer>
      </div>
    </TooltipProvider>
  );
}
