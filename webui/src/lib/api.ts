// lib/api.ts — typed API client for Aether-Vault FastAPI server

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface Commit {
  hash: string;
  message: string;
  author: string;
  timestamp: string | null;
  parent_hash: string | null;
  root_tree_hash: string | null;
  tags: string[];
  metrics: Record<string, number | string>;
  tree?: Record<string, { hash: string; size: number; type: string; layers: unknown[] }>;
}

export interface Ref {
  [branchName: string]: string; // branch -> commit hash
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

export async function fetchRefs(): Promise<Ref> {
  return fetchJSON<Ref>("/api/refs");
}

export async function fetchCommit(hash: string): Promise<Commit> {
  return fetchJSON<Commit>(`/api/commits/${hash}`);
}

export async function fetchStats(): Promise<StorageStats> {
  return fetchJSON<StorageStats>("/api/stats");
}

export async function fetchCommitsForBranches(
  refs: Ref,
  limit = 40
): Promise<Commit[]> {
  const visited = new Set<string>();
  const commits: Commit[] = [];

  // Walk each branch tip through the parent chain
  const queue: string[] = Object.values(refs).filter(Boolean);

  while (queue.length > 0 && commits.length < limit) {
    const hash = queue.shift()!;
    if (!hash || visited.has(hash)) continue;
    visited.add(hash);
    try {
      const c = await fetchCommit(hash);
      commits.push(c);
      if (c.parent_hash) queue.push(c.parent_hash);
    } catch {
      // commit may not exist locally yet
    }
  }

  // Sort by timestamp descending
  commits.sort((a, b) => {
    const ta = a.timestamp ? new Date(a.timestamp).getTime() : 0;
    const tb = b.timestamp ? new Date(b.timestamp).getTime() : 0;
    return tb - ta;
  });

  return commits;
}

export async function fetchDashboardData(): Promise<DashboardData> {
  try {
    const [health, refs, stats] = await Promise.all([
      fetchHealth().catch(() => null),
      fetchRefs().catch(() => ({})),
      fetchStats().catch(() => null),
    ]);

    let commits: Commit[] = [];
    if (refs && Object.keys(refs).length > 0) {
      commits = await fetchCommitsForBranches(refs, 40);
    }

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
