// owner: builder-C
// Card-21b replacement — collapsed by default. 8+ MCP tools manual invocation.
// Builder D wires onInvoke(tool, args) → POST /api/v5/mcp/{tool}.
import { useState } from "react";
import { CollapsibleCard } from "./_Collapsible";

export interface MCPConsoleProps {
  tools?: string[];
  onInvoke?: (tool: string, args: Record<string, unknown>) => Promise<unknown>;
}

export function MCPConsole({ tools = [], onInvoke }: MCPConsoleProps) {
  const [selected, setSelected] = useState<string>(tools[0] ?? "");
  const [argsText, setArgsText] = useState<string>("{}");
  const [output, setOutput] = useState<string>("");
  const [busy, setBusy] = useState(false);

  async function run() {
    if (!onInvoke || !selected) return;
    setBusy(true);
    setOutput("…");
    try {
      const args = JSON.parse(argsText || "{}");
      const res = await onInvoke(selected, args);
      setOutput(JSON.stringify(res, null, 2));
    } catch (e) {
      setOutput(`error: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <CollapsibleCard
      title="mcp console"
      subtitle={`${tools.length} tools`}
    >
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-2">
          <label className="block text-[10px] uppercase tracking-wider text-text-3">
            tool
          </label>
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="w-full rounded border border-stroke-1 bg-bg-inset px-2 py-1 text-xs text-text-1"
          >
            {tools.length === 0 && <option value="">—</option>}
            {tools.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>

          <label className="block text-[10px] uppercase tracking-wider text-text-3">
            args (json)
          </label>
          <textarea
            value={argsText}
            onChange={(e) => setArgsText(e.target.value)}
            rows={6}
            className="num w-full rounded border border-stroke-1 bg-bg-inset px-2 py-1 text-[11px] text-text-1"
          />
          <button
            type="button"
            disabled={busy || !onInvoke || !selected}
            onClick={run}
            className="rounded border border-[color:var(--wong-blue)]/40 bg-[color:var(--wong-blue)]/10 px-3 py-1 text-xs text-[color:var(--wong-blue)] disabled:opacity-40"
          >
            {busy ? "running…" : "invoke"}
          </button>
        </div>

        <div>
          <label className="block text-[10px] uppercase tracking-wider text-text-3">
            output
          </label>
          <pre className="num mt-1 max-h-64 overflow-auto rounded border border-stroke-1 bg-bg-inset p-2 text-[11px] text-text-2">
            {output || "// awaiting invocation"}
          </pre>
        </div>
      </div>
    </CollapsibleCard>
  );
}
