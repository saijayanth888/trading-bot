import * as React from "react";
import { cn } from "@/lib/cn";

export interface StatProps extends React.HTMLAttributes<HTMLDivElement> {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "default" | "pos" | "neg" | "warn" | "acc" | "inf";
  large?: boolean;
}

export function Stat({ label, value, sub, tone = "default", large = false, className, ...props }: StatProps) {
  const valueClass = {
    default: "text-text-1",
    pos: "text-success",
    neg: "text-danger",
    warn: "text-warn",
    acc: "text-accent",
    inf: "text-info",
  }[tone];

  return (
    <div className={cn("flex flex-col gap-1", className)} {...props}>
      <div className="label">{label}</div>
      <div className={cn("num font-semibold tracking-[-0.005em]", large ? "text-[28px] leading-[32px]" : "text-[18px] leading-[22px]", valueClass)}>
        {value}
      </div>
      {sub && <div className="num text-[11px] text-text-3">{sub}</div>}
    </div>
  );
}
