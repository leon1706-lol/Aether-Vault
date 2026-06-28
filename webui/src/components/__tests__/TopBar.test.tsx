import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TopBar } from "../TopBar";

describe("TopBar", () => {
  it("defaults the title to Dashboard when no title prop is passed", () => {
    render(<TopBar health={null} loading={false} onRefresh={vi.fn()} />);
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
  });

  it("renders whichever title is passed, locking in the per-tab title fix", () => {
    render(<TopBar health={null} loading={false} onRefresh={vi.fn()} title="Commits" />);
    expect(screen.getByText("Commits")).toBeInTheDocument();
    expect(screen.queryByText("Dashboard")).not.toBeInTheDocument();
  });

  it("shows the project badge only when a projectName is provided", () => {
    const { rerender } = render(<TopBar health={null} loading={false} onRefresh={vi.fn()} />);
    expect(screen.queryByText(/my-project/)).not.toBeInTheDocument();

    rerender(<TopBar health={null} loading={false} onRefresh={vi.fn()} projectName="my-project" />);
    expect(screen.getByText(/my-project/)).toBeInTheDocument();
  });

  it("shows Server Offline when health is null and not loading", () => {
    render(<TopBar health={null} loading={false} onRefresh={vi.fn()} />);
    expect(screen.getByText("Server Offline")).toBeInTheDocument();
  });

  it("shows Server Online with the version when health is ok", () => {
    render(
      <TopBar health={{ status: "ok", version: "1.4.0" }} loading={false} onRefresh={vi.fn()} />
    );
    expect(screen.getByText("Server Online · v1.4.0")).toBeInTheDocument();
  });
});
