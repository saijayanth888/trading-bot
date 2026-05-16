// owner: builder-C
// Side-link only per spec §1 + §9 (native panel = v2 = `mf-console` ticket).
export interface ModelForgeSideLinkProps {
  href?: string;
  endpointCount?: number | null;
}

export function ModelForgeSideLink({
  href = "http://127.0.0.1:3001/",
  endpointCount = 95,
}: ModelForgeSideLinkProps) {
  return (
    <section
      aria-label="modelforge"
      className="rounded-lg border border-stroke-1 bg-bg-card p-4"
    >
      <h2 className="text-xs uppercase tracking-wider text-text-3">
        modelforge
      </h2>
      <p className="mt-2 text-xs text-text-3">
        {endpointCount ?? "—"} mf-api endpoints. Native panel is a v2 ticket
        (<code className="num text-text-2">mf-console</code>).
      </p>
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        className="mt-3 inline-block rounded border border-[color:var(--wong-blue)]/40 bg-[color:var(--wong-blue)]/10 px-3 py-1 text-xs text-[color:var(--wong-blue)] hover:bg-[color:var(--wong-blue)]/20"
      >
        open modelforge ↗
      </a>
    </section>
  );
}
