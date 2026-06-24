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
import type { Project } from "@/lib/api";

const SELECTED_PROJECT_KEY = "aether-vault:selected-project";

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
        />
        <div className="page-content">
          {active === "weight-diff" ? (
            <WeightDiffPanel projectId={selectedProject?.project_id ?? null} />
          ) : active === "projects" ? (
            <ProjectsPanel selectedProjectId={selectedProject?.project_id ?? null} onOpen={openProject} />
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
