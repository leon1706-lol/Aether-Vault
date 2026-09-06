"use client";

import { useEffect, useState } from "react";
import { useDashboard } from "@/hooks/useDashboard";
import { Sidebar } from "@/components/Sidebar";
import { TopBar } from "@/components/TopBar";
import { StatsRow } from "@/components/StatsRow";
import { CommitGraph } from "@/components/CommitGraph";
import { CommitList } from "@/components/CommitList";
import { BranchList } from "@/components/BranchList";
import { MetricsChart } from "@/components/MetricsChart";
import { WeightDiffPanel } from "@/components/WeightDiffPanel";
import { ProjectsPanel } from "@/components/ProjectsPanel";
import { RunsPanel } from "@/components/RunsPanel";
import { CommitsPanel } from "@/components/CommitsPanel";
import { BranchesPanel } from "@/components/BranchesPanel";
import { MetricsPanel } from "@/components/MetricsPanel";
import { StoragePanel } from "@/components/StoragePanel";
import { ImproverPanel } from "@/components/ImproverPanel";
import { RegressionPanel } from "@/components/RegressionPanel";
import type { Project } from "@/lib/api";

const SELECTED_PROJECT_KEY = "aether-vault:selected-project";

const TAB_TITLES: Record<string, string> = {
  dashboard: "Dashboard",
  commits: "Commits",
  branches: "Branches",
  metrics: "Metrics",
  storage: "Storage",
  "weight-diff": "Weight Diff",
  projects: "Projects",
  runs: "Runs",
  improver: "Improver",
  regression: "Regression",
};

export default function DashboardPage() {
  const [active, setActive] = useState("dashboard");
  // The run id to deep-link into (?run=<id>) — read once on mount and handed down;
  // RunsPanel owns updating this param itself as the user navigates within the tab.
  const [initialRunId, setInitialRunId] = useState<string | null>(null);
  // v1.3.0 (todo.md item 25): same deep-link pattern for the weight-diff tab
  // (?tab=weight-diff&a=<hash>&b=<hash>&path=<relPath>) — read once on mount, handed
  // down; WeightDiffPanel owns updating these params itself as slots/path change.
  const [initialWeightDiff, setInitialWeightDiff] = useState<{
    a: string | null; b: string | null; path: string | null;
  }>({ a: null, b: null, path: null });
  // null = no project selected, dashboard shows every project on the shared registry
  // (preserves the original pre-Projects-tab behavior). Persisted across reloads.
  const [selectedProject, setSelectedProject] = useState<Project | null>(null);

  useEffect(() => {
    const raw = window.localStorage.getItem(SELECTED_PROJECT_KEY);
    if (raw) {
      try {
        setSelectedProject(JSON.parse(raw));
      } catch {
        window.localStorage.removeItem(SELECTED_PROJECT_KEY);
      }
    }
    // Deep linking: ?tab=<id>&run=<id> in the URL — read once after mount (not a lazy
    // useState initializer, to avoid an SSR/client hydration mismatch) so a
    // shared/reloaded link lands on the right tab/run.
    const params = new URLSearchParams(window.location.search);
    const tab = params.get("tab");
    if (tab && tab in TAB_TITLES) setActive(tab);
    setInitialRunId(params.get("run"));
    setInitialWeightDiff({ a: params.get("a"), b: params.get("b"), path: params.get("path") });
  }, []);

  function selectTab(tab: string) {
    setActive(tab);
    const params = new URLSearchParams(window.location.search);
    params.set("tab", tab);
    if (tab !== "runs") params.delete("run"); // leaving the tab drops the deep link
    if (tab !== "weight-diff") {
      params.delete("a");
      params.delete("b");
      params.delete("path");
    }
    window.history.replaceState(null, "", `?${params.toString()}`);
  }

  const { data, loading, refresh } = useDashboard(15000, selectedProject?.project_id ?? null);

  function openProject(project: Project) {
    setSelectedProject(project);
    window.localStorage.setItem(SELECTED_PROJECT_KEY, JSON.stringify(project));
    selectTab("dashboard");
  }

  function clearProject() {
    setSelectedProject(null);
    window.localStorage.removeItem(SELECTED_PROJECT_KEY);
  }

  // v1.3.0 (todo.md item 25): cross-link from a run's linked commits into the weight-diff
  // tab, both slots pre-filled — RunsPanel calls this with (olderHash, newerHash).
  function openWeightDiff(a: string, b: string, path: string | null = null) {
    setInitialWeightDiff({ a, b, path });
    selectTab("weight-diff");
  }

  return (
    <div className="app-shell">
      <Sidebar active={active} onSelect={selectTab} />
      <div className="main-content">
        <TopBar
          health={data?.health ?? null}
          loading={loading}
          onRefresh={refresh}
          projectName={selectedProject?.project_name ?? null}
          onClearProject={clearProject}
          title={TAB_TITLES[active] ?? active}
        />
        <div className="page-content">
          {active === "weight-diff" ? (
            <WeightDiffPanel
              projectId={selectedProject?.project_id ?? null}
              initialSlotAHash={initialWeightDiff.a}
              initialSlotBHash={initialWeightDiff.b}
              initialPath={initialWeightDiff.path}
            />
          ) : active === "projects" ? (
            <ProjectsPanel selectedProjectId={selectedProject?.project_id ?? null} onOpen={openProject} />
          ) : active === "runs" ? (
            <RunsPanel
              projectId={selectedProject?.project_id ?? null}
              initialRunId={initialRunId}
              onCompareWeights={openWeightDiff}
            />
          ) : active === "improver" ? (
            <ImproverPanel projectId={selectedProject?.project_id ?? null} />
          ) : active === "regression" ? (
            <RegressionPanel projectId={selectedProject?.project_id ?? null} />
          ) : active === "commits" ? (
            <CommitsPanel refs={data?.refs ?? {}} projectId={selectedProject?.project_id ?? null} />
          ) : active === "branches" ? (
            <BranchesPanel
              refs={data?.refs ?? {}}
              commits={data?.commits ?? []}
              loading={loading}
              projectId={selectedProject?.project_id ?? null}
              error={data?.error ?? null}
            />
          ) : active === "metrics" ? (
            <MetricsPanel
              commits={data?.commits ?? []}
              refs={data?.refs ?? {}}
              loading={loading}
              error={data?.error ?? null}
            />
          ) : active === "storage" ? (
            <StoragePanel
              stats={data?.stats ?? null}
              commits={data?.commits ?? []}
              loading={loading}
              projectId={selectedProject?.project_id ?? null}
            />
          ) : active === "dashboard" ? (
            <>
              {/* Stats row */}
              <div className="section fade-in fade-in-1">
                <StatsRow data={data} loading={loading} />
              </div>

              {/* Commit graph + Branches */}
              <div className="grid-2 section fade-in fade-in-2">
                <CommitGraph commits={data?.commits ?? []} loading={loading} error={data?.error ?? null} />
                <BranchList
                  refs={data?.refs ?? {}}
                  commits={data?.commits ?? []}
                  loading={loading}
                  error={data?.error ?? null}
                />
              </div>

              {/* Metrics chart + Commit log */}
              <div className="grid-2 section fade-in fade-in-3">
                <MetricsChart commits={data?.commits ?? []} loading={loading} error={data?.error ?? null} />
                <CommitList commits={data?.commits ?? []} loading={loading} error={data?.error ?? null} />
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
