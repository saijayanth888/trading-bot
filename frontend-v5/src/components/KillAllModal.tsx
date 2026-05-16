// owner: builder-C
// Type-to-confirm KILL modal — per spec §4.3 Kill-switch UX + frontend-debate
// G8 + NNGroup pattern. Confirm is DISABLED until the operator types `KILL`
// into the textbox. NO default focus on Confirm — initial focus goes to the
// textbox (which is on the cancel/no-action path until typed).
import * as Dialog from "@radix-ui/react-dialog";
import { useState, useRef, useEffect } from "react";
import { cn } from "@/lib/cn";

export interface KillAllModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm?: () => void | Promise<void>;
}

const REQUIRED = "KILL";

export function KillAllModal({
  open,
  onOpenChange,
  onConfirm,
}: KillAllModalProps) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setText("");
      setBusy(false);
    }
  }, [open]);

  const enabled = text === REQUIRED && !busy;

  async function handleConfirm() {
    if (!enabled || !onConfirm) return;
    setBusy(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/60" />
        <Dialog.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(420px,90vw)]",
            "-translate-x-1/2 -translate-y-1/2",
            "rounded-lg border border-[color:var(--wong-vermillion)]/40",
            "bg-bg-card p-5",
          )}
          // No default focus on Confirm: route initial focus to the textbox.
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            inputRef.current?.focus();
          }}
        >
          <Dialog.Title className="text-base font-semibold text-[color:var(--wong-vermillion)]">
            ⛔ KILL ALL
          </Dialog.Title>
          <Dialog.Description className="mt-1 text-xs text-text-2">
            This will pause crypto AND flatten all stocks positions. The action
            is composite and not auto-reversible.
          </Dialog.Description>

          <label className="mt-4 block text-[10px] uppercase tracking-wider text-text-3">
            type <code className="num text-text-1">KILL</code> to enable confirm
          </label>
          <input
            ref={inputRef}
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            className="num mt-1 w-full rounded border border-stroke-2 bg-bg-inset px-3 py-2 text-sm text-text-1 outline-none focus:border-[color:var(--wong-vermillion)]/60"
          />

          <div className="mt-5 flex items-center justify-end gap-2">
            <Dialog.Close asChild>
              <button
                type="button"
                className="rounded border border-stroke-2 px-3 py-1 text-xs text-text-2 hover:bg-bg-inset"
              >
                cancel
              </button>
            </Dialog.Close>
            <button
              type="button"
              disabled={!enabled}
              onClick={handleConfirm}
              // explicit: do NOT autofocus this button
              tabIndex={enabled ? 0 : -1}
              className={cn(
                "rounded border px-3 py-1 text-xs font-semibold",
                enabled
                  ? "border-[color:var(--wong-vermillion)]/60 bg-[color:var(--wong-vermillion)]/20 text-[color:var(--wong-vermillion)] hover:bg-[color:var(--wong-vermillion)]/30"
                  : "border-stroke-2 bg-bg-inset text-text-4 cursor-not-allowed",
              )}
            >
              {busy ? "killing…" : "confirm kill"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
