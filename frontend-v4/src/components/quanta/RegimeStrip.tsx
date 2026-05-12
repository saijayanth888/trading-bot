import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Stat } from "@/components/ui/stat";
import { apiGet, endpoints } from "@/lib/api";

interface RegimeShape {
  regime?: string;
  confidence?: number;
  asset?: string;
  hold_time?: string;
}

export function RegimeStrip() {
  const q = useQuery({
    queryKey: ["regime"],
    queryFn: () => apiGet<RegimeShape>(endpoints.regime),
    refetchInterval: 60_000,
  });
  const r = q.data;
  const conf = r?.confidence ?? 0;
  const regime = (r?.regime ?? "unknown").replace(/_/g, " ");
  const tone = regime.includes("up") ? "success" : regime.includes("down") ? "danger" : regime.includes("mean") ? "info" : regime.includes("high") ? "warn" : "default";

  return (
    <Card>
      <CardHeader tag="0b" title="Regime · live" trailing={<Chip tone={tone}>{regime.toUpperCase()}</Chip>} />
      <CardBody className="grid grid-cols-3 gap-4">
        <Stat label="Confidence" value={`${(conf * 100).toFixed(0)}%`} tone={conf > 0.6 ? "pos" : "default"} />
        <Stat label="Asset" value={r?.asset ?? "—"} />
        <Stat label="Held" value={r?.hold_time ?? "—"} />
      </CardBody>
    </Card>
  );
}
