// lib/api.ts — typed API client for Aether-Vault FastAPI server

const API_BASE =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// "Protected" mode support — see TokenGate.tsx. The token lives in localStorage, not a
// build-time env var, since it's set at runtime long after the webui image is built.
export const API_TOKEN_STORAGE_KEY = "aether-vault:api-token";

export function getStoredApiToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(API_TOKEN_STORAGE_KEY);
}

export function setStoredApiToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(API_TOKEN_STORAGE_KEY, token);
}

export function clearStoredApiToken(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(API_TOKEN_STORAGE_KEY);
}

export class UnauthorizedError extends Error {
  constructor() {
    super("This registry is protected — a valid access token is required.");
    this.name = "UnauthorizedError";
  }
}

// Set once by TokenGate.tsx on mount — lets fetchJSON surface a 401 as "show the token entry
// screen" without every call site needing to know about it individually.
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

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
  // Merge commits list every parent here (the server reconstructs parent_hash +
  // extra_parents into one array); optional so fixtures/older payloads without it still
  // render through the parent_hash fallback.
  parents?: string[];
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
  const token = getStoredApiToken();
  const headers = new Headers(options?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${API_BASE}${path}`, {
    next: { revalidate: 0 },
    ...options,
    headers,
  });
  if (res.status === 401) {
    onUnauthorized?.();
    throw new UnauthorizedError();
  }
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

// ---------------------------------------------------------------------------
// Runs (v1.2.0) — first-class experiment grouping on the server
// ---------------------------------------------------------------------------

export interface Run {
  id: string;
  project_id: string;
  name: string | null;
  status: "created" | "running" | "completed" | "failed";
  parent_run_id: string | null;
  created_by: string | null;
  metrics_summary: Record<string, number | string>;
  created_at: string | null;
  completed_at: string | null;
  commit_hashes?: string[];
  // v1.2.2 env snapshot/replay: content id of the run's environment snapshot object
  // (fetchable via GET /api/objects/{id}; `av replay <run-id>` renders it).
  env_snapshot_id?: string | null;
  // v1.2.5: opt-in pointer to a published .avh context-memory object (`av handoff
  // --publish`) — null unless the repo owner explicitly published one.
  avh_object_id?: string | null;
  // The most recent `av promote`/merge policy decision made for this run's active
  // commit; null until the first decision for this run.
  policy_outcome?: { decision: "allow" | "deny"; rule: string | null; at: string } | null;
}

// Server-computed semantic summary shape returned by GET /api/runs/{id}/summary — a
// smaller sibling of runDetail.ts's client-side TreeDiffSummary (no chunk-dedup/
// layer-movement fields). null when the run has fewer than two linked commits with tree data.
export interface RunServerSemanticSummary {
  files: { added: string[]; removed: string[]; changed: string[] };
  totals: { bytes_before: number; bytes_after: number };
  summary: string;
}

export interface RunLineageEntry {
  id: string;
  name: string | null;
  status: Run["status"];
}

export interface RunSummary {
  run: Run;
  lineage: RunLineageEntry[];
  commits: Array<{
    hash: string;
    message: string;
    metrics: Record<string, number | string>;
    timestamp: string | null;
  }>;
  total_commits: number;
  semantic_summary: RunServerSemanticSummary | null;
  env_snapshot_id: string | null;
  avh_object_id: string | null;
}

// One request for the run-detail view (lineage + linked commits + a server-computed
// semantic summary); runDetail.ts's pure client-side functions stay as the
// fallback/test surface.
export async function fetchRunSummary(runId: string): Promise<RunSummary> {
  return fetchJSON<RunSummary>(`/api/runs/${encodeURIComponent(runId)}/summary`);
}

export interface RunMetricPoint {
  hash: string;
  message: string;
  metrics: Record<string, number | string>;
  timestamp: string | null;
  linked_at: string | null;
}

// The full per-commit metric series for a run, oldest-linked-first, paging through the
// uncapped GET /api/runs/{id}/metrics endpoint. `maxPoints` bounds memory use so a
// runaway run with tens of thousands of linked commits can't hang the tab.
export async function fetchRunMetrics(
  runId: string,
  { pageLimit = 200, maxPoints = 5000 }: { pageLimit?: number; maxPoints?: number } = {}
): Promise<RunMetricPoint[]> {
  const points: RunMetricPoint[] = [];
  let cursor: string | null = null;
  do {
    const params = new URLSearchParams({ limit: String(pageLimit) });
    if (cursor) params.set("cursor", cursor);
    const page: { points: RunMetricPoint[]; next_cursor: string | null } = await fetchJSON(
      `/api/runs/${encodeURIComponent(runId)}/metrics?${params.toString()}`
    );
    points.push(...page.points);
    cursor = page.next_cursor;
  } while (cursor && points.length < maxPoints);
  return points;
}

export async function fetchRuns(
  opts: { projectId?: string | null; status?: string; limit?: number } = {}
): Promise<Run[]> {
  const params = new URLSearchParams();
  if (opts.projectId) params.set("project_id", opts.projectId);
  if (opts.status) params.set("status", opts.status);
  params.set("limit", String(opts.limit ?? 50));
  const data = await fetchJSON<{ runs: Run[] }>(`/api/runs?${params.toString()}`);
  return data.runs ?? [];
}

// Single run incl. linked commit hashes; the panel composes lineage/metrics/semantic
// summary client-side from this + fetchCommit.
export async function fetchRun(runId: string): Promise<Run> {
  return fetchJSON<Run>(`/api/runs/${encodeURIComponent(runId)}`);
}

// Lightweight poll target for the live badge: newest event id (0 when none yet).
export async function fetchLatestEventId(): Promise<number> {
  const data = await fetchJSON<{ events: { id: number }[] }>("/api/events?limit=1");
  return data.events?.[0]?.id ?? 0;
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

// Fetch the most recent commits in a single request. The server already returns them
// newest-first with parent_hash for graph edges, so there's no need to walk the parent
// chain one commit at a time.
export async function fetchCommits(limit = 40, projectId?: string | null): Promise<Commit[]> {
  const qs = projectId ? `&project_id=${encodeURIComponent(projectId)}` : "";
  const data = await fetchJSON<CommitListResponse>(`/api/commits?limit=${limit}${qs}`);
  return data.commits ?? [];
}

// Same endpoint as fetchCommits, but with ?include_layers=true — returns full per-commit
// trees in one request. Capped lower than fetchCommits' default since each commit's
// response is much heavier with its full tree attached.
export async function fetchCommitsWithLayers(limit = 30, projectId?: string | null): Promise<Commit[]> {
  const qs = projectId ? `&project_id=${encodeURIComponent(projectId)}` : "";
  const data = await fetchJSON<CommitListResponse>(
    `/api/commits?limit=${limit}&include_layers=true${qs}`
  );
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

// projectId is optional — when unset, the dashboard shows commits/refs from every
// project on this shared registry. Stats stay unscoped: they describe the shared object
// store, which is deliberately deduplicated across projects.
export async function fetchDashboardData(projectId?: string | null): Promise<DashboardData> {
  // Each sub-fetch catches its own rejection (a safe empty fallback so one flaky
  // endpoint doesn't blank the whole dashboard) but must still record the failure, or a
  // genuinely empty registry and an unreachable one would look identical to every panel.
  const failures: string[] = [];
  const describe = (label: string) => (err: unknown) => {
    failures.push(`${label}: ${err instanceof Error ? err.message : String(err)}`);
  };

  try {
    const [health, refs, stats, commits] = await Promise.all([
      fetchHealth().catch((err) => {
        describe("health")(err);
        return null;
      }),
      fetchRefs(projectId).catch((err) => {
        describe("refs")(err);
        return {};
      }),
      fetchStats().catch((err) => {
        describe("stats")(err);
        return null;
      }),
      fetchCommits(40, projectId).catch((err) => {
        describe("commits")(err);
        return [];
      }),
    ]);

    return {
      health, refs, commits, stats,
      error: failures.length ? failures.join("; ") : null,
    };
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

// ---------------------------------------------------------------------------
// RSI control plane: improver lineage, self-edits, canaries, and the anomaly feed — the
// WebUI counterparts of `av improver`/`av canary`/the server-side anomaly detectors.
// ---------------------------------------------------------------------------

export interface ImproverVersion {
  id: string;
  project_id: string;
  manifest_object_id: string;
  parent_id: string | null;
  created_by: string | null;
  created_at: string | null;
}

export type ChangeSetStatus = "proposed" | "approved" | "rejected" | "applied" | "rolled_back";

export interface ChangeSet {
  id: string;
  project_id: string;
  improver_id: string | null;
  object_id: string;
  status: ChangeSetStatus;
  risk: "low" | "medium" | "high" | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface CanaryResult {
  id: number;
  project_id: string;
  improver_id: string;
  suite_object_id: string;
  passed: boolean;
  details: unknown;
  run_id: string | null;
  created_at: string | null;
}

export interface AnomalyEvent {
  id: number;
  ts: string | null;
  kind: string;
  project_id: string | null;
  payload: Record<string, unknown> & { type?: string };
}

export async function fetchImproverVersions(
  opts: { projectId?: string | null; limit?: number } = {}
): Promise<ImproverVersion[]> {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 50) });
  if (opts.projectId) params.set("project_id", opts.projectId);
  const data = await fetchJSON<{ improvers: ImproverVersion[] }>(`/api/improvers?${params.toString()}`);
  return data.improvers ?? [];
}

export async function fetchImproverLineage(improverId: string, depth = 50): Promise<ImproverVersion[]> {
  const data = await fetchJSON<{ improver_id: string; lineage: ImproverVersion[] }>(
    `/api/improvers/${encodeURIComponent(improverId)}/lineage?depth=${depth}`
  );
  return data.lineage ?? [];
}

export async function fetchChangeSets(
  opts: { projectId?: string | null; status?: ChangeSetStatus; limit?: number } = {}
): Promise<ChangeSet[]> {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 50) });
  if (opts.projectId) params.set("project_id", opts.projectId);
  if (opts.status) params.set("status", opts.status);
  const data = await fetchJSON<{ change_sets: ChangeSet[] }>(`/api/change-sets?${params.toString()}`);
  return data.change_sets ?? [];
}

export async function fetchCanaryResults(
  opts: { projectId?: string | null; improverId?: string; limit?: number } = {}
): Promise<CanaryResult[]> {
  const params = new URLSearchParams({ limit: String(opts.limit ?? 50) });
  if (opts.projectId) params.set("project_id", opts.projectId);
  if (opts.improverId) params.set("improver_id", opts.improverId);
  const data = await fetchJSON<{ canary_results: CanaryResult[] }>(`/api/canary-results?${params.toString()}`);
  return data.canary_results ?? [];
}

export async function fetchAnomalyEvents(
  opts: { projectId?: string | null; since?: number; limit?: number } = {}
): Promise<AnomalyEvent[]> {
  const params = new URLSearchParams({
    kinds: "anomaly", since: String(opts.since ?? 0), limit: String(opts.limit ?? 50),
  });
  if (opts.projectId) params.set("project_id", opts.projectId);
  const data = await fetchJSON<{ events: AnomalyEvent[] }>(`/api/events?${params.toString()}`);
  return data.events ?? [];
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
