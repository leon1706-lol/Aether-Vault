"use client";

import { useState } from "react";
import { useDashboard } from "@/hooks/useDashboard";
import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { StatsRow } from "@/components/StatsRow";
import { CommitGraph } from "@/components/CommitGraph";
import { CommitList } from "@/components/CommitList";
import { BranchList } from "@/components/BranchList";
import { MetricsChart } from "@/components/MetricsChart";
import { WeightDiffPanel } from "@/components/WeightDiffPanel";

export default function DashboardPage() {
  const { data, loading, refresh } = useDashboard(15000);
  const [active, setActive] = useState("dashboard");

  return (
    <div className="app-shell">
      <Sidebar active={active} onSelect={setActive} />
      <div className="main-content">
        <TopBar
          health={data?.health ?? null}
          loading={loading}
          onRefresh={refresh}
        />
        <div className="page-content">
          {active === "weight-diff" ? (
            <WeightDiffPanel />
          ) : (
            <>
              {/* Stats row */}
              <div className="section fade-in fade-in-1">
                <StatsRow data={data} loading={loading} />
              </div>

              {/* Commit graph + Branches */}
              <div className="grid-2 section fade-in fade-in-2">
                <CommitGraph commits={data?.commits ?? []} loading={loading} />
                <BranchList refs={data?.refs ?? {}} commits={data?.commits ?? []} loading={loading} />
              </div>

              {/* Metrics chart + Commit log */}
              <div className="grid-2 section fade-in fade-in-3">
                <MetricsChart commits={data?.commits ?? []} loading={loading} />
                <CommitList commits={data?.commits ?? []} loading={loading} />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
