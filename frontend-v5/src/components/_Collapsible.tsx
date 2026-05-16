// owner: builder-C
// Thin wrapper over @radix-ui/react-collapsible — the shadcn-style Collapsible
// without dragging in the whole shadcn registry. Three v1 surfaces use it:
// RegimeConfigEditor, DecisionAudit, MCPConsole (all collapsed by default
// per spec §3 + operator scope on G3).
import * as RC from "@radix-ui/react-collapsible";
import type { ReactNode } from "react";

export interface CollapsibleCardProps {
  title: string;
  subtitle?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}

export function CollapsibleCard({
  title,
  subtitle,
  defaultOpen = false,
  children,
}: CollapsibleCardProps) {
  return (
    <RC.Root
      defaultOpen={defaultOpen}
      className="rounded-lg border border-stroke-1 bg-bg-card"
    >
      <RC.Trigger className="group flex w-full items-center justify-between px-4 py-2 text-left hover:bg-bg-inset/40">
        <span className="flex items-baseline gap-2">
          <span className="text-xs uppercase tracking-wider text-text-2">
            {title}
          </span>
          {subtitle && (
            <span className="text-[10px] text-text-3">{subtitle}</span>
          )}
        </span>
        <span className="text-[10px] uppercase tracking-wider text-text-3 group-data-[state=open]:rotate-90 transition-transform">
          ▸
        </span>
      </RC.Trigger>
      <RC.Content className="border-t border-stroke-1 p-4">
        {children}
      </RC.Content>
    </RC.Root>
  );
}
