// owner: builder-D (temporary; builder-C may replace layout — preserve hook call)
// Fixed bottom-right dock per spec §4.3 + frontend-debate G2/G8.
// KILL flow uses useKillFlow state machine. Modal traps focus on the type-to-
// confirm input (NOT the Confirm button) per G8 + NNGroup pattern.
import { useEffect, useRef } from "react";
import { useKillFlow, useStrategyAction } from "@/hooks/useKillFlow";
import { cn } from "@/lib/cn";

export function Intervene() {
  const kill = useKillFlow();
  const pause = useStrategyAction("pause");

  return (
    <>
      <aside
        aria-label="intervene"
        className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 rounded-lg border border-stroke-1 bg-[color:var(--bg-overlay)] p-3 backdrop-blur shadow-lg"
      >
        <button
          type="button"
          onClick={kill.arm}
          disabled={kill.state === "executing"}
          className={cn(
            "rounded border border-[color:var(--status-halt)]/40",
            "bg-[color:var(--status-halt)]/10 px-3 py-2 text-xs font-semibold",
            "text-[color:var(--status-halt)] hover:bg-[color:var(--status-halt)]/20",
            "disabled:opacity-40 disabled:cursor-not-allowed",
          )}
        >
          ⛔ KILL ALL
        </button>
        <button
          type="button"
          onClick={() => pause.fire("crypto-v4")}
          disabled={pause.busy}
          className="rounded border border-stroke-2 px-3 py-1 text-[10px] uppercase tracking-wider text-text-2 hover:border-[color:var(--wong-orange)]/40 hover:text-[color:var(--wong-orange)] disabled:opacity-40"
        >
          ⏸ pause crypto
        </button>
        <button
          type="button"
          onClick={() => pause.fire("stocks-wheel")}
          disabled={pause.busy}
          className="rounded border border-stroke-2 px-3 py-1 text-[10px] uppercase tracking-wider text-text-2 hover:border-[color:var(--wong-orange)]/40 hover:text-[color:var(--wong-orange)] disabled:opacity-40"
        >
          ⏸ pause stocks
        </button>
        {pause.error && (
          <span className="text-[10px] text-[color:var(--status-halt)]">
            {pause.error}
          </span>
        )}
      </aside>

      {kill.state === "confirming" && <KillModal kill={kill} />}
      {kill.state === "executing" && (
        <KillBackdrop label="executing kill…" />
      )}
      {kill.state === "done" && (
        <KillBackdrop label="kill complete" onDismiss={kill.reset} ok />
      )}
    </>
  );
}

function KillModal({ kill }: { kill: ReturnType<typeof useKillFlow> }) {
  // Focus the textbox (NOT Confirm) on open per frontend-debate G8.
  const inputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="kill-modal-title"
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur"
      onKeyDown={(e) => {
        if (e.key === "Escape") kill.cancel();
      }}
    >
      <div className="w-[440px] rounded-lg border border-[color:var(--status-halt)]/40 bg-bg-card p-5 shadow-xl">
        <h3
          id="kill-modal-title"
          className="text-sm font-semibold text-[color:var(--status-halt)]"
        >
          ⛔ kill-all confirm
        </h3>
        <p className="mt-2 text-xs text-text-2">
          This will pause crypto-v4 AND flatten all stocks positions
          immediately. Type <span className="num text-text-1">KILL</span> to
          enable the Confirm button.
        </p>
        <input
          ref={inputRef}
          type="text"
          value={kill.typed}
          onChange={(e) => kill.setTyped(e.target.value)}
          placeholder="type KILL"
          aria-label="type KILL to enable confirm"
          className="mt-3 w-full rounded border border-stroke-2 bg-bg-inset px-3 py-2 text-sm num text-text-1 focus:border-[color:var(--status-halt)] focus:outline-none"
        />
        {kill.error && (
          <p className="mt-2 text-[11px] text-[color:var(--status-halt)]">
            {kill.error}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={kill.cancel}
            className="rounded border border-stroke-2 px-3 py-1 text-xs text-text-2 hover:text-text-1"
          >
            cancel
          </button>
          <button
            type="button"
            onClick={() => void kill.confirm()}
            disabled={!kill.canConfirm}
            // Intentionally NOT autoFocus — operator must reach Confirm
            // through the textbox per G8.
            className={cn(
              "rounded border px-3 py-1 text-xs font-semibold",
              kill.canConfirm
                ? "border-[color:var(--status-halt)] bg-[color:var(--status-halt)] text-white hover:opacity-90"
                : "border-stroke-2 text-text-4 cursor-not-allowed",
            )}
          >
            confirm kill
          </button>
        </div>
      </div>
    </div>
  );
}

function KillBackdrop({
  label,
  ok,
  onDismiss,
}: {
  label: string;
  ok?: boolean;
  onDismiss?: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur">
      <div
        className={cn(
          "rounded-lg border bg-bg-card p-5 shadow-xl",
          ok
            ? "border-[color:var(--wong-blue)]/40"
            : "border-[color:var(--status-halt)]/40",
        )}
      >
        <p className="text-sm text-text-1">{label}</p>
        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            className="mt-3 rounded border border-stroke-2 px-3 py-1 text-xs text-text-2 hover:text-text-1"
          >
            dismiss
          </button>
        )}
      </div>
    </div>
  );
}
