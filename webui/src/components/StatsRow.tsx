"use client";

import { formatBytes, type DashboardData } from "@/lib/api";

interface Props {
  data: DashboardData | null;
  loading: boolean;
}

export function StatsRow({ data, loading }: Props) {
  const totalCommits = data?.commits?.length ?? 0;
  const totalBranches = Object.keys(data?.refs ?? {}).length;
  const totalTags = [
    ...new Set(data?.commits?.flatMap((c) => c.tags ?? []) ?? []),
  ].length;
  const storageBytes = data?.stats?.total_size_bytes ?? 0;
  const totalObjects = data?.stats?.total_objects ?? data?.stats?.object_count ?? 0;

  // Collect all unique metric keys
  const metricKeys = [
    ...new Set(
      data?.commits?.flatMap((c) => Object.keys(c.metrics ?? {})) ?? []
    ),
  ];

  // Find best (latest) value for the first numeric metric found
  const primaryMetricKey = metricKeys.find((k) => {
    const vals = data?.commits?.map((c) => c.metrics?.[k]).filter((v) => typeof v === "number");
    return vals && vals.length > 0;
  });
  const primaryMetricVal = primaryMetricKey
    ? data?.commits?.find(
        (c) => typeof c.metrics?.[primaryMetricKey] === "number"
      )?.metrics?.[primaryMetricKey]
    : null;

  const cards = [
    {
      label: "Total Commits",
      value: loading ? "—" : totalCommits.toString(),
      sub: "in all branches",
      accent: "accent-orange",
    },
    {
      label: "Branches",
      value: loading ? "—" : totalBranches.toString(),
      sub: "active refs",
      accent: "accent-orange-soft",
    },
    {
      label: "Unique Tags",
      value: loading ? "—" : totalTags.toString(),
      sub: "across all commits",
      accent: "accent-teal",
    },
    {
      label: "CAS Objects",
      value: loading ? "—" : totalObjects.toLocaleString(),
      sub: formatBytes(storageBytes),
      accent: "accent-amber",
    },
    ...(primaryMetricKey && primaryMetricVal !== null
      ? [
          {
            label: primaryMetricKey.toUpperCase(),
            value: loading
              ? "—"
              : typeof primaryMetricVal === "number"
              ? primaryMetricVal.toFixed(3)
              : String(primaryMetricVal),
            sub: "latest commit metric",
            accent: "accent-green" as string,
          },
        ]
      : []),
  ];

  return (
    <div className="stats-grid">
      {cards.map((card, i) => (
        <div key={card.label} className={`stat-card fade-in fade-in-${i + 1}`}>
          <div className="stat-label">{card.label}</div>
          <div className={`stat-value ${card.accent}`}>{card.value}</div>
          <div className="stat-sub">{card.sub}</div>
        </div>
      ))}
    </div>
  );
}
