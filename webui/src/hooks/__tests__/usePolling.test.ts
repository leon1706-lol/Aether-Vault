// V1.6.3: visibility-aware polling with exponential backoff -- the mechanism every
// dashboard timer now runs on.
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import { usePolling } from "../usePolling";

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "visibilityState", { value: hidden ? "hidden" : "visible", configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("usePolling", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
    setHidden(false);
  });

  it("runs immediately, then at the interval while successful", async () => {
    const fn = vi.fn(async () => true);
    renderHook(() => usePolling(fn, 1_000));
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));
    await act(async () => { vi.advanceTimersByTime(1_000); });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(2));
    await act(async () => { vi.advanceTimersByTime(1_000); });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(3));
  });

  it("backs off 1x -> 2x -> 4x on consecutive failures and resets on success", async () => {
    let ok = false;
    const fn = vi.fn(async () => ok);
    const { result } = renderHook(() => usePolling(fn, 1_000, { maxBackoffMs: 3_000 }));
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.nextDelayMs).toBe(2_000)); // 1 failure -> 2x

    await act(async () => { vi.advanceTimersByTime(1_000); });
    expect(fn).toHaveBeenCalledTimes(1); // not yet: backed off
    await act(async () => { vi.advanceTimersByTime(1_000); });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.nextDelayMs).toBe(3_000)); // 2 failures -> 4x, capped at 3s

    ok = true;
    await act(async () => { vi.advanceTimersByTime(3_000); });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(result.current.nextDelayMs).toBe(1_000)); // reset
    expect(result.current.failures).toBe(0);
  });

  it("treats a thrown error as a failure", async () => {
    const fn = vi.fn(async () => { throw new Error("boom"); });
    const { result } = renderHook(() => usePolling(fn, 1_000));
    await waitFor(() => expect(result.current.failures).toBe(1));
    expect(result.current.nextDelayMs).toBe(2_000);
  });

  it("pauses while the document is hidden and refreshes on return", async () => {
    const fn = vi.fn(async () => true);
    const { result } = renderHook(() => usePolling(fn, 1_000));
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));

    await act(async () => { setHidden(true); });
    await waitFor(() => expect(result.current.paused).toBe(true));
    await act(async () => { vi.advanceTimersByTime(10_000); });
    expect(fn).toHaveBeenCalledTimes(1); // nothing while hidden

    await act(async () => { setHidden(false); });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(2)); // immediate refresh on return
    await waitFor(() => expect(result.current.paused).toBe(false));
  });

  it("refetches immediately when fn identity changes (new project)", async () => {
    const a = vi.fn(async () => true);
    const b = vi.fn(async () => true);
    const { rerender } = renderHook(({ fn }: { fn: () => Promise<boolean> }) => usePolling(fn, 60_000), {
      initialProps: { fn: a },
    });
    await waitFor(() => expect(a).toHaveBeenCalledTimes(1));
    rerender({ fn: b });
    await waitFor(() => expect(b).toHaveBeenCalledTimes(1));
  });

  it("immediate:false waits one interval before the first call", async () => {
    const fn = vi.fn(async () => true);
    renderHook(() => usePolling(fn, 1_000, { immediate: false }));
    expect(fn).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(1_000); });
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));
  });

  it("stops after unmount", async () => {
    const fn = vi.fn(async () => true);
    const { unmount } = renderHook(() => usePolling(fn, 1_000));
    await waitFor(() => expect(fn).toHaveBeenCalledTimes(1));
    unmount();
    await act(async () => { vi.advanceTimersByTime(5_000); });
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
