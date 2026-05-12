import * as React from "react";
import { NavLink } from "react-router-dom";
import {
  Activity,
  Gauge,
  GitBranch,
  Layers,
  LineChart,
  Brain,
  BookOpen,
  Sigma,
  ExternalLink,
} from "lucide-react";
import { cn } from "@/lib/cn";

interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  num: string;
  external?: boolean;
}

const GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: "Monitor",
    items: [
      { to: "/", label: "Overview", icon: Activity, num: "1" },
      { to: "/debate", label: "Debate", icon: Brain, num: "2" },
      { to: "/risk", label: "Risk · MC", icon: Sigma, num: "3" },
    ],
  },
  {
    title: "Models",
    items: [
      { to: "/adapters", label: "Adapters", icon: GitBranch, num: "4" },
      { to: "/parity", label: "Parity", icon: LineChart, num: "5" },
    ],
  },
  {
    title: "Universe",
    items: [
      { to: "/screening", label: "27 names", icon: Layers, num: "6" },
      { to: "/weekly", label: "Weekly", icon: BookOpen, num: "7" },
    ],
  },
  {
    title: "System",
    items: [
      { to: "/diagnostics", label: "Diagnostics", icon: Gauge, num: "8" },
      { to: "/ops", label: "Legacy /ops", icon: ExternalLink, num: "9", external: true },
    ],
  },
];

export function Sidebar() {
  return (
    <aside className="sticky top-14 hidden h-[calc(100vh-3.5rem)] w-[200px] shrink-0 flex-col border-r border-stroke-1 bg-bg-rail px-3 py-4 md:flex">
      <nav className="flex-1 space-y-5 overflow-y-auto scrollbar-thin">
        {GROUPS.map((g) => (
          <div key={g.title}>
            <div className="px-2 pb-1 text-[9px] font-semibold uppercase tracking-[0.16em] text-text-4">
              {g.title}
            </div>
            <ul className="space-y-0.5">
              {g.items.map((it) => (
                <li key={it.to}>
                  {it.external ? (
                    <a
                      href={it.to}
                      className="group flex items-center gap-2.5 rounded-[6px] px-2 py-1.5 text-[12px] text-text-3 hover:bg-bg-inset hover:text-text-1"
                    >
                      <NumChip n={it.num} />
                      <it.icon className="h-3.5 w-3.5 opacity-60" />
                      <span>{it.label}</span>
                      <ExternalLink className="ml-auto h-3 w-3 opacity-40" />
                    </a>
                  ) : (
                    <NavLink to={it.to} end className={({ isActive }) => navCls(isActive)}>
                      <NumChip n={it.num} />
                      <it.icon className="h-3.5 w-3.5 opacity-70" />
                      <span>{it.label}</span>
                    </NavLink>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      <div className="mt-4 rounded-[8px] border border-stroke-1 bg-bg-card p-3">
        <div className="label">Operator</div>
        <div className="num text-[12px] font-medium text-text-1">quant@quanta</div>
        <div className="num text-[10px] text-text-3">127.0.0.1:5173</div>
      </div>
    </aside>
  );
}

function NumChip({ n }: { n: string }) {
  return (
    <span className="grid h-[18px] w-[18px] place-items-center rounded-[4px] border border-stroke-2 bg-bg-card text-[10px] font-mono tabular-nums text-text-4">
      {n}
    </span>
  );
}

function navCls(active: boolean): string {
  return cn(
    "group flex items-center gap-2.5 rounded-[6px] px-2 py-1.5 text-[12px] transition-colors",
    active
      ? "bg-accent-bg text-text-1 border-l-2 border-accent pl-[6px]"
      : "text-text-3 hover:bg-bg-inset hover:text-text-1",
  );
}
