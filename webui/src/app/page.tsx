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
};

export default function DashboardPage() {
  const [active, setActive] = useState("dashboard");
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
  }, []);

  const { data, loading, refresh } = useDashboard(15000, selectedProject?.project_id ?? null);

  function openProject(project: Project) {
    setSelectedProject(project);
    window.localStorage.setItem(SELECTED_PROJECT_KEY, JSON.stringify(project));
    setActive("dashboard");
  }

  function clearProject() {
    setSelectedProject(null);
    window.localStorage.removeItem(SELECTED_PROJECT_KEY);
  }

  return (
    <div className="app-shell">
      <Sidebar active={active} onSelect={setActive} />
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
            <WeightDiffPanel projectId={selectedProject?.project_id ?? null} />
          ) : active === "projects" ? (
            <ProjectsPanel selectedProjectId={selectedProject?.project_id ?? null} onOpen={openProject} />
          ) : active === "runs" ? (
            <RunsPanel projectId={selectedProject?.project_id ?? null} />
          ) : active === "commits" ? (
            <CommitsPanel refs={data?.refs ?? {}} projectId={selectedProject?.project_id ?? null} />
          ) : active === "branches" ? (
            <BranchesPanel
              refs={data?.refs ?? {}}
              commits={data?.commits ?? []}
              loading={loading}
              projectId={selectedProject?.project_id ?? null}
            />
          ) : active === "metrics" ? (
            <MetricsPanel commits={data?.commits ?? []} refs={data?.refs ?? {}} loading={loading} />
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
                <CommitGraph commits={data?.commits ?? []} loading={loading} />
                <BranchList refs={data?.refs ?? {}} commits={data?.commits ?? []} loading={loading} />
              </div>

              {/* Metrics chart + Commit log */}
              <div className="grid-2 section fade-in fade-in-3">
                <MetricsChart commits={data?.commits ?? []} loading={loading} />
                <CommitList commits={data?.commits ?? []} loading={loading} />
              </div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
