"use client";

import { useEffect, useState } from "react";
import { fetchProjects, type Project } from "@/lib/api";

interface Props {
  selectedProjectId: string | null;
  onOpen: (project: Project) => void;
}

export function ProjectsPanel({ selectedProjectId, onOpen }: Props) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProjects()
      .then((p) => {
        if (!cancelled) setProjects(p);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load projects");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="card">
      <div className="section-header">
        <span className="card-title">
          <ProjectsIcon />
          Projects
        </span>
        <span className="section-count">{projects.length}</span>
      </div>

      {loading ? (
        <div className="loading-overlay">
          <div className="spinner" />
          Loading projects…
        </div>
      ) : error ? (
        <div className="empty-state">Failed to load projects: {error}</div>
      ) : projects.length === 0 ? (
        <div className="empty-state">
          <ProjectsIcon size={32} />
          <span>No projects have pushed to this registry yet</span>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Run <code style={{ fontSize: 11 }}>av init</code> then{" "}
            <code style={{ fontSize: 11 }}>av commit</code> in a repo to register one.
          </span>
        </div>
      ) : (
        <div className="checkpoint-list" style={{ maxHeight: "none" }}>
          {projects.map((p) => (
            <ProjectRow
              key={p.project_id}
              project={p}
              active={p.project_id === selectedProjectId}
              onOpen={() => onOpen(p)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ProjectRow({
  project,
  active,
  onOpen,
}: {
  project: Project;
  active: boolean;
  onOpen: () => void;
}) {
  const lastPush = project.last_push ? formatRelative(new Date(project.last_push)) : "never";
  return (
    <div className={`checkpoint-row ${active ? "checkpoint-row--selected" : ""}`} style={{ padding: "10px 8px" }}>
      <span className="checkpoint-iter" title="Project ID">
        {project.project_id.slice(0, 8)}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 13.5, fontWeight: 500, color: "var(--text-primary)" }}>
          {project.project_name}
        </div>
        <div style={{ fontSize: 11.5, color: "var(--text-secondary)" }}>
          {project.commit_count} commit{project.commit_count === 1 ? "" : "s"} · last push {lastPush}
        </div>
      </div>
      <button type="button" className="btn btn-ghost" style={{ padding: "5px 12px" }} onClick={onOpen}>
        {active ? "Active" : "Open"}
      </button>
    </div>
  );
}

function formatRelative(date: Date): string {
  const now = Date.now();
  const diff = now - date.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return date.toLocaleDateString();
}

function ProjectsIcon({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size} height={size}
      viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
    >
      <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z" />
    </svg>
  );
}
