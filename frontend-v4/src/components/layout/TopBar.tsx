import * as React from "react";
import { Sun, Moon, RotateCw, ShieldOff } from "lucide-react";
import { Chip } from "@/components/ui/chip";
import { Button } from "@/components/ui/button";
import { useUi } from "@/store/ui";
import { fmtMoney, fmtPct } from "@/lib/format";

interface TopBarProps {
  combinedEquity?: number;
  dayPct?: number;
  mode?: string;
}

export function TopBar({ combinedEquity, dayPct, mode = "PAPER" }: TopBarProps) {
  const { theme, toggleTheme } = useUi();
  const [now, setNow] = React.useState<Date>(() => new Date());

  React.useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const etTime = React.useMemo(() => {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
      hour12: true,
    }).format(now);
  }, [now]);

  const dayCls = dayPct == null ? "text-text-3" : dayPct >= 0 ? "text-success" : "text-danger";

  return (
    <header className="sticky top-0 z-40 flex h-14 items-center gap-6 border-b border-stroke-1 bg-bg-page/95 px-5 backdrop-blur">
      <div className="flex items-center gap-3">
        <div className="grid h-8 w-8 place-items-center rounded-[8px] bg-accent-bg text-accent font-mono font-semibold">
          Q
        </div>
        <div className="leading-tight">
          <div className="text-[13px] font-semibold tracking-[-0.005em]">
            QUANTA <span className="text-text-3">·</span> V4
          </div>
          <div className="text-[10px] uppercase tracking-[0.10em] text-text-3">
            operator console · wave-2
          </div>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <Chip tone="info">{mode}</Chip>
        <Chip tone="success">DEBATE-30s</Chip>
        <Chip tone="accent">OLLAMA</Chip>
      </div>

      <div className="ml-auto flex items-center gap-6">
        <div className="flex flex-col items-end leading-tight">
          <div className="label">Combined equity</div>
          <div className="num text-[15px] font-semibold">
            {combinedEquity != null ? fmtMoney(combinedEquity) : "—"}
          </div>
          <div className={`num text-[11px] ${dayCls}`}>{dayPct != null ? `${fmtPct(dayPct)} day` : "— day"}</div>
        </div>
        <div className="flex flex-col items-end leading-tight">
          <div className="label">Clock · ET</div>
          <div className="num text-[15px] font-semibold tabular-nums">{etTime}</div>
          <div className="text-[10px] uppercase tracking-[0.10em] text-text-3">ET · UTC−4</div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="icon" onClick={() => location.reload()} title="Reload">
            <RotateCw className="h-4 w-4" />
          </Button>
          <Button variant="danger" size="sm" title="Hold 1.5s to flatten and halt">
            <ShieldOff className="h-3.5 w-3.5" />
            KILL · ARM
          </Button>
          <Button variant="ghost" size="icon" onClick={toggleTheme} title="Toggle theme">
            {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </Button>
        </div>
      </div>
    </header>
  );
}
