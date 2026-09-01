// v1.2.5: ProjectsPanel had no test coverage at all — closing that gap alongside the
// useDashboard hook test (the other identified untested surface).
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ProjectsPanel } from "../ProjectsPanel";
import * as api from "../../lib/api";

vi.mock("../../lib/api", () => ({
  fetchProjects: vi.fn(),
}));

const mocked = vi.mocked(api);

function project(overrides: Partial<api.Project> = {}): api.Project {
  return {
    project_id: "abcdef1234567890",
    project_name: "my-model",
    commit_count: 3,
    last_push: null,
    ...overrides,
  };
}

describe("ProjectsPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a loading state before the fetch resolves", () => {
    mocked.fetchProjects.mockReturnValue(new Promise(() => {})); // never resolves
    render(<ProjectsPanel selectedProjectId={null} onOpen={vi.fn()} />);
    expect(screen.getByText(/Loading projects/i)).toBeInTheDocument();
  });

  it("shows an empty state when the registry has no projects", async () => {
    mocked.fetchProjects.mockResolvedValue([]);
    render(<ProjectsPanel selectedProjectId={null} onOpen={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/No projects have pushed/i)).toBeInTheDocument()
    );
  });

  it("surfaces a fetch error instead of pretending everything is fine", async () => {
    mocked.fetchProjects.mockRejectedValue(new Error("HTTP 500"));
    render(<ProjectsPanel selectedProjectId={null} onOpen={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/Failed to load projects: HTTP 500/)).toBeInTheDocument()
    );
  });

  it("lists projects with commit counts and a count badge", async () => {
    mocked.fetchProjects.mockResolvedValue([
      project({ project_id: "aaa111", project_name: "model-a", commit_count: 5 }),
      project({ project_id: "bbb222", project_name: "model-b", commit_count: 1 }),
    ]);
    render(<ProjectsPanel selectedProjectId={null} onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("model-a")).toBeInTheDocument());
    expect(screen.getByText("model-b")).toBeInTheDocument();
    expect(screen.getByText(/5 commits/)).toBeInTheDocument();
    expect(screen.getByText(/1 commit\b/)).toBeInTheDocument(); // singular, not "1 commits"
    expect(screen.getByText("2")).toBeInTheDocument(); // section-count badge
  });

  it("marks the currently-selected project as Active instead of Open", async () => {
    mocked.fetchProjects.mockResolvedValue([
      project({ project_id: "selected-1", project_name: "current" }),
      project({ project_id: "other-1", project_name: "other" }),
    ]);
    render(<ProjectsPanel selectedProjectId="selected-1" onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText("current")).toBeInTheDocument());
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Open")).toBeInTheDocument();
  });

  it("calls onOpen with the clicked project", async () => {
    const onOpen = vi.fn();
    const p = project({ project_id: "click-me", project_name: "clickable" });
    mocked.fetchProjects.mockResolvedValue([p]);
    render(<ProjectsPanel selectedProjectId={null} onOpen={onOpen} />);
    await waitFor(() => expect(screen.getByText("clickable")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Open"));
    expect(onOpen).toHaveBeenCalledWith(p);
  });

  it("renders a relative 'last push' time, falling back to 'never'", async () => {
    mocked.fetchProjects.mockResolvedValue([
      project({ project_id: "p1", project_name: "no-push-yet", last_push: null }),
    ]);
    render(<ProjectsPanel selectedProjectId={null} onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/last push never/)).toBeInTheDocument());
  });
});
