import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

const chipVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full px-2 py-[2px] text-[11px] font-medium uppercase tracking-[0.06em] num",
  {
    variants: {
      tone: {
        default: "bg-bg-inset text-text-2 border border-stroke-1",
        success: "bg-success-bg text-success border border-success-line",
        danger: "bg-danger-bg text-danger border border-danger-line",
        warn: "bg-warn-bg text-warn border border-warn-line",
        accent: "bg-accent-bg text-accent border border-accent-line",
        info: "bg-info-bg text-info border border-info-line",
      },
    },
    defaultVariants: { tone: "default" },
  },
);

export interface ChipProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof chipVariants> {
  dot?: boolean;
}

export function Chip({ className, tone, dot = true, children, ...props }: ChipProps) {
  return (
    <span className={cn(chipVariants({ tone }), className)} {...props}>
      {dot && (
        <span
          className={cn("inline-block h-1.5 w-1.5 rounded-full", {
            "bg-text-3": tone === "default" || tone == null,
            "bg-success": tone === "success",
            "bg-danger": tone === "danger",
            "bg-warn": tone === "warn",
            "bg-accent": tone === "accent",
            "bg-info": tone === "info",
          })}
        />
      )}
      {children}
    </span>
  );
}
