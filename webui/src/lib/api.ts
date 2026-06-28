// lib/api.ts — typed API client for Aether-Vault FastAPI server

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface TreeLayer {
  name: string;
  hash: string;
  size: number;
}

export interface Commit {
  hash: string;
  message: string;
  author: string;
  timestamp: string | null;
  parent_hash: string | null;
  root_tree_hash: string | null;
  tags: string[];
  metrics: Record<string, number | string>;
  tree?: Record<string, { hash: string; size: number; type: string; layers: TreeLayer[] }>;
  project_id?: string;
  project_name?: string;
}

export interface Ref {
  [branchName: string]: string; // branch -> commit hash
}

export interface Project {
  project_id: string;
  project_name: string;
  commit_count: number;
  last_push: string | null;
}

export interface HealthResponse {
  status: "ok" | "error";
  version: string;
}

export interface StorageStats {
  total_objects: number;
  total_size_bytes: number;
  object_count?: number;
}

export interface GCResult {
  status: string;
  alive_objects: number;
  deleted_objects: number;
  reused_trees: number;
}

export interface DashboardData {
  health: HealthResponse | null;
  refs: Ref;
  commits: Commit[];
  stats: StorageStats | null;
  error: string | null;
}

async function fetchJSON<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    next: { revalidate: 0 },
    ...options,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
  return res.json() as Promise<T>;
}

export async function fetchHealth(): Promise<HealthResponse> {
  return fetchJSON<HealthResponse>("/api/health");
}

export async function fetchRefs(projectId?: string | null): Promise<Ref> {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return fetchJSON<Ref>(`/api/refs${qs}`);
}

export async function fetchProjects(): Promise<Project[]> {
  const data = await fetchJSON<{ projects: Project[] }>("/api/projects");
  return data.projects ?? [];
}

// ref_name may itself contain a "/" (project_id/branch namespacing) — the server's
// {ref_name:path} route expects that slash literal in the URL path, not percent-encoded,
// so this intentionally does not encodeURIComponent the whole name (matches how
// python/av_cli/client.py builds the same URL).
export async function createRef(refName: string, commitHash: string): Promise<void> {
  await fetchJSON(`/api/refs/${refName}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ commit_hash: commitHash }),
  });
}

export async function fetchCommit(hash: string): Promise<Commit> {
  return fetchJSON<Commit>(`/api/commits/${hash}`);
}

export async function fetchStats(): Promise<StorageStats> {
  return fetchJSON<StorageStats>("/api/stats");
}

export interface CommitListResponse {
  commits: Commit[];
  total: number;
  limit: number;
  offset: number;
  next_offset: number | null;
}

// Fetch the most recent commits in a SINGLE request. The server already returns them
// newest-first with parent_hash for graph edges, so there is no need to walk the parent
// chain one commit at a time (the previous fetchCommitsForBranches did N sequential
// round-trips — a request waterfall that scaled with history length).
export async function fetchCommits(limit = 40, projectId?: string | null): Promise<Commit[]> {
  const qs = projectId ? `&project_id=${encodeURIComponent(projectId)}` : "";
  const data = await fetchJSON<CommitListResponse>(`/api/commits?limit=${limit}${qs}`);
  return data.commits ?? [];
}

// Same endpoint as fetchCommits, but offset-aware and returning the pagination envelope
// (next_offset/total) so a panel can implement its own "Load more" without changing the
// shared fixed-window fetchCommits used by the dashboard hook.
export async function fetchCommitsPage(
  limit = 40,
  offset = 0,
  projectId?: string | null
): Promise<CommitListResponse> {
  const qs = projectId ? `&project_id=${encodeURIComponent(projectId)}` : "";
  return fetchJSON<CommitListResponse>(`/api/commits?limit=${limit}&offset=${offset}${qs}`);
}

// projectId is optional — when unset, the dashboard shows commits/refs from every project
// on this shared registry (the pre-existing behavior), exactly as before the Projects tab
// was added. Stats stay unscoped: they describe the shared object store, which is
// deliberately deduplicated *across* projects (see development/Probleme.md).
export async function fetchDashboardData(projectId?: string | null): Promise<DashboardData> {
  try {
    const [health, refs, stats, commits] = await Promise.all([
      fetchHealth().catch(() => null),
      fetchRefs(projectId).catch(() => ({})),
      fetchStats().catch(() => null),
      fetchCommits(40, projectId).catch(() => []),
    ]);

    return { health, refs, commits, stats, error: null };
  } catch (err) {
    return {
      health: null,
      refs: {},
      commits: [],
      stats: null,
      error: err instanceof Error ? err.message : "Unknown error",
    };
  }
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

export function shortHash(hash: string): string {
  return hash.slice(0, 7);
}
