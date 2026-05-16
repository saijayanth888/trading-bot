// owner: builder-C
// Card-19 replacement — collapsed by default. Builder D wires:
//   - useRegimeConfig() → GET /api/v5/regime_config
//   - onSave(patch)    → POST /api/v5/regime_config
import { CollapsibleCard } from "./_Collapsible";

export interface RegimeConfigEditorProps {
  config?: Record<string, unknown> | null;
  onSave?: (patch: Record<string, unknown>) => void;
}

export function RegimeConfigEditor({ config }: RegimeConfigEditorProps) {
  return (
    <CollapsibleCard
      title="regime config editor"
      subtitle="advanced — expand to edit"
    >
      <div className="space-y-2 text-xs text-text-3">
        <p>
          Per-side regime-detector parameters. Edits POST to{" "}
          <code className="num text-text-2">/api/v5/regime_config</code>.
        </p>
        <pre className="num overflow-auto rounded border border-stroke-1 bg-bg-inset p-2 text-[11px] text-text-2 max-h-64">
          {config ? JSON.stringify(config, null, 2) : "// loading…"}
        </pre>
      </div>
    </CollapsibleCard>
  );
}
