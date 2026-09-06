import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TokenGate } from "../TokenGate";
import { getStoredApiToken } from "@/lib/api";

// Exercises the real 401-triggers-the-prompt path end-to-end by mocking global fetch
// to return a 401 and calling a real fetchJSON-backed function, rather than mocking
// lib/api.ts itself.
import * as apiModule from "@/lib/api";

beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState({}, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TokenGate", () => {
  it("renders children normally when nothing is protected", () => {
    render(
      <TokenGate>
        <div>dashboard content</div>
      </TokenGate>
    );
    expect(screen.getByText("dashboard content")).toBeInTheDocument();
    expect(screen.queryByText("This registry is protected")).not.toBeInTheDocument();
  });

  it("saves a token handed off via the av_token URL param and strips it from the URL", async () => {
    window.history.replaceState({}, "", "/?av_token=handoff-token-123");

    render(
      <TokenGate>
        <div>content</div>
      </TokenGate>
    );

    await waitFor(() => expect(getStoredApiToken()).toBe("handoff-token-123"));
    expect(window.location.search).not.toContain("av_token");
  });

  it("does not show the manual prompt when a token was just handed off via the URL", () => {
    window.history.replaceState({}, "", "/?av_token=handoff-token-123");
    render(
      <TokenGate>
        <div>content</div>
      </TokenGate>
    );
    expect(screen.queryByText("This registry is protected")).not.toBeInTheDocument();
  });

  it("shows the manual entry prompt when a 401 is reported", async () => {
    render(
      <TokenGate>
        <div>content</div>
      </TokenGate>
    );

    vi.spyOn(global, "fetch").mockResolvedValue(new Response(null, { status: 401 }));
    await expect(apiModule.fetchHealth()).rejects.toThrow(apiModule.UnauthorizedError);

    await waitFor(() => expect(screen.getByText("This registry is protected")).toBeInTheDocument());
  });

  it("rejects an empty submit without saving or reloading", async () => {
    const user = userEvent.setup();
    render(
      <TokenGate>
        <div>content</div>
      </TokenGate>
    );
    vi.spyOn(global, "fetch").mockResolvedValue(new Response(null, { status: 401 }));
    await expect(apiModule.fetchHealth()).rejects.toThrow();
    await waitFor(() => expect(screen.getByText("This registry is protected")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(document.querySelector(".diff-warning")).toBeInTheDocument();
    expect(getStoredApiToken()).toBeNull();
  });

  it("saves the entered token and reloads on submit", async () => {
    const user = userEvent.setup();
    const reloadSpy = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload: reloadSpy },
    });

    render(
      <TokenGate>
        <div>content</div>
      </TokenGate>
    );
    vi.spyOn(global, "fetch").mockResolvedValue(new Response(null, { status: 401 }));
    await expect(apiModule.fetchHealth()).rejects.toThrow();
    await waitFor(() => expect(screen.getByText("This registry is protected")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("Access token"), "my-new-token");
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(getStoredApiToken()).toBe("my-new-token");
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });
});
