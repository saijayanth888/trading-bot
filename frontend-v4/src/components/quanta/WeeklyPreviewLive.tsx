import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { marked } from "marked";
import DOMPurify from "dompurify";
import { Calendar, Clock, FileText } from "lucide-react";
import { Card, CardHeader, CardBody, CardFooter } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Stat } from "@/components/ui/stat";
import { apiGet, endpoints } from "@/lib/api";
import type { WeeklyPreview } from "@/types/v4";
import { fmtMoney, fmtPct, fmtAgo } from "@/lib/format";

// marked: GFM-like; output is sanitized via DOMPurify before injection
// because even though the publisher renders its own template, the embedded
// debate transcripts and reflector lessons come from LLM output and could
// (in theory) contain hostile HTML. Belt + braces.
marked.setOptions({ gfm: true, breaks: false });

const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    "h1", "h2", "h3", "h4", "p", "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li", "br", "hr", "details", "summary", "a", "table", "thead",
    "tbody", "tr", "th", "td", "span",
  ],
  ALLOWED_ATTR: ["href", "class"],
  RETURN_TRUSTED_TYPE: false as const,
};

export function WeeklyPreviewLive() {
  const q = useQuery({
    queryKey: ["v4", "weekly"],
    queryFn: () => apiGet<WeeklyPreview>(endpoints.v4_weekly_preview),
    refetchInterval: 60_000,
  });
  const data = q.data;

  const sanitizedHtml = React.useMemo(() => {
    if (!data) return "";
    const rendered = marked.parse(data.markdown);
    const raw = typeof rendered === "string" ? rendered : "";
    const out = DOMPurify.sanitize(raw, PURIFY_CONFIG);
    return typeof out === "string" ? out : String(out);
  }, [data]);

  const dayUntilFriday = React.useMemo(() => {
    const now = new Date();
    const day = now.getUTCDay(); // 5 == Friday
    return ((5 - day + 7) % 7) || 0;
  }, []);

  return (
    <Card>
      <CardHeader
        tag="7"
        title="Weekly publisher · live preview"
        trailing={
          <>
            <Chip tone={dayUntilFriday === 0 ? "warn" : "info"}>
              <Calendar className="h-3 w-3" />
              {dayUntilFriday === 0 ? "Friday — publishes 16:00 ET" : `T-${dayUntilFriday}d`}
            </Chip>
            <Chip tone={data?.run_mode === "live" ? "danger" : "info"}>
              {data?.run_mode?.toUpperCase() ?? "—"}
            </Chip>
          </>
        }
      />
      <CardBody className="space-y-4">
        {data && (
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat
              label="Net P&L"
              value={fmtMoney(data.net_pnl)}
              sub={fmtPct(data.net_pnl_pct)}
              tone={data.net_pnl >= 0 ? "pos" : "neg"}
            />
            <Stat
              label="Drawdown"
              value={fmtPct(-Math.abs(data.drawdown_pct))}
              tone={data.drawdown_pct > 5 ? "neg" : "default"}
            />
            <Stat label="Open positions" value={data.open_count.toString()} />
            <Stat label="Trades" value={data.trade_count.toString()} />
          </div>
        )}

        <div className="rounded-[10px] border border-stroke-1 bg-bg-card-2 p-4">
          {!data && q.isLoading && <p className="text-[12px] text-text-3">Loading preview…</p>}
          {!data && q.isError && (
            <p className="text-[12px] text-danger">Preview unavailable — weekly publisher offline?</p>
          )}
          {data && <SafeMarkdown html={sanitizedHtml} />}
        </div>
      </CardBody>
      <CardFooter className="flex items-center justify-between">
        <span className="flex items-center gap-1.5">
          <FileText className="h-3 w-3" />
          docs/weekly/{data?.iso_week ?? "—"}.md (preview only · not written)
        </span>
        <span className="num flex items-center gap-1.5">
          <Clock className="h-3 w-3" />
          generated {data ? fmtAgo(data.generated_ts) : "—"}
        </span>
      </CardFooter>
    </Card>
  );
}

/**
 * Renders pre-sanitized Markdown HTML. The `html` prop MUST have been run
 * through DOMPurify.sanitize() before reaching this component (see callers).
 */
function SafeMarkdown({ html }: { html: string }) {
  // html is DOMPurify-sanitized at the call site; the inner ref injects the
  // cleaned tree into the prose container without React parsing JSX.
  return (
    <div
      className="prose prose-invert max-w-none text-[13px] leading-relaxed
        [&>h1]:text-[18px] [&>h1]:font-semibold [&>h1]:mb-3
        [&>h2]:text-[14px] [&>h2]:font-semibold [&>h2]:mt-5 [&>h2]:mb-2 [&>h2]:uppercase [&>h2]:tracking-[0.10em] [&>h2]:text-text-2
        [&>h3]:text-[13px] [&>h3]:font-semibold [&>h3]:text-text-1 [&>h3]:mt-4 [&>h3]:mb-1
        [&_code]:font-mono [&_code]:text-[12px] [&_code]:bg-bg-inset [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded-sm
        [&_ul]:list-disc [&_ul]:pl-6 [&_li]:my-1
        [&_details]:mt-2 [&_summary]:cursor-pointer [&_summary]:text-text-2"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
