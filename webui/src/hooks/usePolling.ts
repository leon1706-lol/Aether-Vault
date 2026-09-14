"use client";

import { useEffect, useState } from "react";

export interface PollingOptions {
  /** Upper bound for the error backoff (default 120 s). */
  maxBackoffMs?: number;
  /** Run once immediately on mount (default true). */
  immediate?: boolean;
  /** Skip polling entirely (e.g. a panel that isn't mounted yet). */
  enabled?: boolean;
}

export interface PollingState {
  /** True while the document is hidden and no timer is armed. */
  paused: boolean;
  /** The delay the next tick was scheduled with (grows on consecutive failures). */
  nextDelayMs: number;
  /** Consecutive failures so far (resets on success). */
  failures: number;
}

/**
 * Visibility-aware polling with exponential backoff (V1.6.3).
 *
 * The dashboard used to fire 4 requests every 15 s (plus the Runs tab's own two timers)
 * for as long as the tab existed -- backgrounded tabs included -- and kept hammering an
 * unreachable registry at full rate. This hook:
 *   - runs `fn` at `intervalMs`, rescheduling with `setTimeout` after each completion
 *     (never overlapping ticks the way `setInterval` can when a tick outlives the interval);
 *   - doubles the delay on each consecutive failure (`fn` throwing, or resolving `false`),
 *     capped at `maxBackoffMs`, and resets to `intervalMs` on the first success;
 *   - stops while `document.hidden` and refreshes immediately when the tab is visible again.
 *
 * `fn` may return a boolean to signal a "soft" failure (a fetch that swallowed its own
 * error but recorded one, like fetchDashboardData) -- `false` counts as a failure.
 */
export function usePolling(
  fn: () => Promise<boolean | void>,
  intervalMs: number,
  options: PollingOptions = {},
): PollingState {
  const { maxBackoffMs = 120_000, immediate = true, enabled = true } = options;
  // `fn` is an effect dependency on purpose: callers pass a useCallback keyed on their
  // inputs (projectId), so a new identity means "different data source -- refetch now",
  // exactly what the setInterval effects this replaces did.
  const [state, setState] = useState<PollingState>({ paused: false, nextDelayMs: intervalMs, failures: 0 });

  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;
    let failures = 0;
    let inFlight = false;

    const hidden = () => typeof document !== "undefined" && document.visibilityState === "hidden";

    const schedule = (delay: number) => {
      if (cancelled) return;
      if (timer) clearTimeout(timer);
      if (hidden()) {
        timer = null;
        setState({ paused: true, nextDelayMs: delay, failures });
        return;
      }
      setState({ paused: false, nextDelayMs: delay, failures });
      timer = setTimeout(tick, delay);
    };

    const tick = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      let ok = true;
      try {
        const result = await fn();
        ok = result !== false;
      } catch {
        ok = false;
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      failures = ok ? 0 : failures + 1;
      const delay = ok ? intervalMs : Math.min(intervalMs * 2 ** failures, maxBackoffMs);
      schedule(delay);
    };

    const onVisibility = () => {
      if (cancelled) return;
      if (hidden()) {
        if (timer) clearTimeout(timer);
        timer = null;
        setState((s) => ({ ...s, paused: true }));
      } else {
        // Back in view: refresh now rather than waiting out whatever delay was pending.
        void tick();
      }
    };

    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibility);
    }
    if (immediate) {
      void tick();
    } else {
      schedule(intervalMs);
    }

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibility);
      }
    };
  }, [fn, intervalMs, maxBackoffMs, immediate, enabled]);

  return state;
}
