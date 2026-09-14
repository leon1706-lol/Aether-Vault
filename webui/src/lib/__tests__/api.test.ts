import { describe, expect, it, vi } from "vitest";

import { fetchRefs, formatBytes, shortHash } from "../api";

// V1.6.3: the dashboard polls /api/refs; it must ask for a bounded page.
describe("fetchRefs", () => {
  it("requests a bounded page (limit=200 by default) and scopes by project", async () => {
    const calls: string[] = [];
    const original = globalThis.fetch;
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return new Response(JSON.stringify({ "p/main": "a".repeat(64) }), {
        status: 200, headers: { "Content-Type": "application/json" },
      });
    }) as unknown as typeof fetch;
    try {
      const refs = await fetchRefs("p");
      expect(refs["p/main"]).toBe("a".repeat(64));
      expect(calls[0]).toContain("/api/refs?");
      expect(calls[0]).toContain("project_id=p");
      expect(calls[0]).toContain("limit=200");
      await fetchRefs(null, 50);
      expect(calls[1]).toContain("limit=50");
      expect(calls[1]).not.toContain("project_id");
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe("formatBytes", () => {
  it("formats zero bytes as '0 B'", () => {
    expect(formatBytes(0)).toBe("0 B");
  });

  it("formats bytes below 1 KB as B", () => {
    expect(formatBytes(512)).toBe("512 B");
  });

  it("formats exact unit boundaries", () => {
    expect(formatBytes(1024)).toBe("1 KB");
    expect(formatBytes(1024 * 1024)).toBe("1 MB");
    expect(formatBytes(1024 * 1024 * 1024)).toBe("1 GB");
  });

  it("rounds to one decimal place for non-exact sizes", () => {
    expect(formatBytes(1536)).toBe("1.5 KB"); // 1.5 KB exactly
    expect(formatBytes(2_500_000)).toBe("2.4 MB");
  });
});

describe("shortHash", () => {
  it("truncates a full 64-char hash to 7 characters", () => {
    const full = "a".repeat(64);
    expect(shortHash(full)).toBe("aaaaaaa");
    expect(shortHash(full)).toHaveLength(7);
  });

  it("returns the whole string unchanged if shorter than 7 characters", () => {
    expect(shortHash("abc")).toBe("abc");
  });
});
