import * as React from "react";
import { cn } from "@/lib/cn";

export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("card flex flex-col", className)} {...props} />
  ),
);
Card.displayName = "Card";

export const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement> & { tag?: string; title: string; trailing?: React.ReactNode }>(
  ({ className, tag, title, trailing, ...props }, ref) => (
    <div ref={ref} className={cn("card-head", className)} {...props}>
      {tag && <span className="num text-[10px] text-text-4 w-6 tabular-nums">{tag}</span>}
      <h2 className="text-[15px] font-semibold tracking-[-0.005em] text-text-1">{title}</h2>
      {trailing && <div className="ml-auto flex items-center gap-2">{trailing}</div>}
    </div>
  ),
);
CardHeader.displayName = "CardHeader";

export const CardBody = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("card-body", className)} {...props} />
  ),
);
CardBody.displayName = "CardBody";

export const CardFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("border-t border-stroke-1 px-[14px] py-[10px] text-[11px] text-text-3", className)} {...props} />
  ),
);
CardFooter.displayName = "CardFooter";
