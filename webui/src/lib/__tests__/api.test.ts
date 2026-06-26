import { describe, expect, it } from "vitest";

import { formatBytes, shortHash } from "../api";

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
