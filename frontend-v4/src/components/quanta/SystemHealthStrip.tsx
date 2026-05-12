import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Chip } from "@/components/ui/chip";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { apiGet, endpoints } from "@/lib/api";

interface Services {
  services: { name: string; status: string; endpoint?: string; meta?: string }[];
}

export function SystemHealthStrip() {
  const q = useQuery({
    queryKey: ["services"],
    queryFn: () => apiGet<Services>(endpoints.services),
    refetchInterval: 30_000,
  });
  const services = q.data?.services ?? [];
  const up = services.filter((s) => s.status === "up" || s.status === "ok").length;

  return (
    <Card>
      <CardHeader
        tag="8"
        title="System health"
        trailing={<Chip tone={up === services.length && services.length > 0 ? "success" : "warn"}>{up}/{services.length} up</Chip>}
      />
      <CardBody className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {services.map((s) => {
          const ok = s.status === "up" || s.status === "ok";
          return (
            <div key={s.name} className="flex items-center gap-2 rounded-[6px] border border-stroke-1 bg-bg-card-2 px-2.5 py-1.5">
              <span className={`h-1.5 w-1.5 rounded-full ${ok ? "bg-success" : "bg-danger"}`} />
              <span className="num text-[12px] font-medium text-text-1">{s.name}</span>
              <span className="num ml-auto text-[10px] text-text-3">{s.meta ?? s.status}</span>
            </div>
          );
        })}
        {services.length === 0 && q.isLoading && (
          <div className="col-span-full text-[12px] text-text-3">Probing services…</div>
        )}
      </CardBody>
    </Card>
  );
}
