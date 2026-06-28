import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CommitsPanel } from "../CommitsPanel";
import type { Commit } from "@/lib/api";

const { fetchCommitsPage, fetchCommit } = vi.hoisted(() => ({
  fetchCommitsPage: vi.fn(),
  fetchCommit: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchCommitsPage, fetchCommit };
});

function makeCommit(overrides: Partial<Commit> = {}): Commit {
  return {
    hash: "a".repeat(64),
    message: "first commit",
    author: "tester",
    timestamp: "2026-06-26T00:00:00Z",
    parent_hash: null,
    root_tree_hash: "b".repeat(64),
    tags: [],
    metrics: {},
    ...overrides,
  };
}

describe("CommitsPanel", () => {
  it("loads and renders the first page of commits", async () => {
    const commit = makeCommit({ message: "implement feature X" });
    fetchCommitsPage.mockResolvedValueOnce({
      commits: [commit],
      total: 1,
      limit: 50,
      offset: 0,
      next_offset: null,
    });

    render(<CommitsPanel refs={{ main: commit.hash }} />);

    expect(screen.getByText("Loading commits…")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("implement feature X")).toBeInTheDocument());
    expect(screen.queryByText("Load more")).not.toBeInTheDocument();
  });

  it("shows a Load more button when the server reports more pages", async () => {
    const commit = makeCommit();
    fetchCommitsPage.mockResolvedValueOnce({
      commits: [commit],
      total: 2,
      limit: 50,
      offset: 0,
      next_offset: 50,
    });

    render(<CommitsPanel refs={{}} />);
    await waitFor(() => expect(screen.getByText("Load more")).toBeInTheDocument());
  });

  it("filters the loaded commits by the search box", async () => {
    const user = userEvent.setup();
    const a = makeCommit({ hash: "a".repeat(64), message: "fix bug in loader" });
    const b = makeCommit({ hash: "c".repeat(64), message: "add new feature" });
    fetchCommitsPage.mockResolvedValueOnce({
      commits: [a, b],
      total: 2,
      limit: 50,
      offset: 0,
      next_offset: null,
    });

    render(<CommitsPanel refs={{}} />);
    await waitFor(() => expect(screen.getByText("fix bug in loader")).toBeInTheDocument());

    await user.type(
      screen.getByPlaceholderText("Search loaded commits by message, author, or tag…"),
      "feature"
    );

    expect(screen.queryByText("fix bug in loader")).not.toBeInTheDocument();
    expect(screen.getByText("add new feature")).toBeInTheDocument();
  });

  it("expanding a commit row lazily fetches and shows its file tree", async () => {
    const user = userEvent.setup();
    const commit = makeCommit({
      hash: "a".repeat(64),
      tree: { "model.bin": { hash: "x".repeat(64), size: 100, type: "blob", layers: [] } },
    });
    fetchCommitsPage.mockResolvedValueOnce({
      commits: [commit],
      total: 1,
      limit: 50,
      offset: 0,
      next_offset: null,
    });
    fetchCommit.mockResolvedValueOnce(commit);

    render(<CommitsPanel refs={{}} />);
    await waitFor(() => expect(screen.getByText(commit.message)).toBeInTheDocument());

    await user.click(screen.getByText(commit.message));

    await waitFor(() => expect(screen.getByText("model.bin")).toBeInTheDocument());
    expect(screen.getByText("added")).toBeInTheDocument();
    expect(fetchCommit).toHaveBeenCalledWith(commit.hash);
  });
});
