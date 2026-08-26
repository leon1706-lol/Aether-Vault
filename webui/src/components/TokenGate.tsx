"use client";

import { useEffect, useState } from "react";
import { setStoredApiToken, setUnauthorizedHandler } from "@/lib/api";

interface Props {
  children: React.ReactNode;
}

// Gates the whole dashboard behind the shared-secret access token when the server is running
// in "Protected" mode. Two ways a token reaches this component:
//
// 1. Launched via `av webui` while the CLI already has one configured (`.av/config`'s
//    remote_api_token) — docker_runtime._open_browser() appends it as a one-time `?av_token=`
//    query param. Saved to localStorage and stripped from the URL immediately on mount, so it
//    never lingers in browser history longer than necessary and the manual prompt below never
//    appears in this case.
// 2. Opened directly (a bookmark, a teammate's own browser that's never seen the token) — any
//    API call's 401 (see lib/api.ts's fetchJSON) triggers the manual entry prompt via
//    setUnauthorizedHandler, registered once here on mount.
export function TokenGate({ children }: Props) {
  const [showPrompt, setShowPrompt] = useState(false);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Consume the one-time handoff token DURING RENDER (guarded to the browser), not in
  // an effect: child panels mount and fire their first fetches BEFORE this component's
  // useEffect ever runs (React runs child effects first), so an effect-based consume
  // raced the very requests it was meant to authenticate — first wave went out
  // unauthenticated and data only appeared after the next poll interval (Probleme.md
  // #79). Writing localStorage here is idempotent and happens-before any child fetch.
  if (typeof window !== "undefined") {
    const url = new URL(window.location.href);
    const handoffToken = url.searchParams.get("av_token");
    if (handoffToken) {
      setStoredApiToken(handoffToken);
      url.searchParams.delete("av_token");
      window.history.replaceState({}, "", url.toString());
    }
  }

  // Strip safety-net AFTER hydration: Next.js patches window.history.replaceState to
  // integrate with the App Router, and a replaceState issued BEFORE hydration finished
  // can be overridden when hydration completes — restoring the entry URL with the
  // ?av_token= param still in the address bar (token itself already persisted, so only
  // the cosmetic strip was lost; caught by webui-e2e's token-gate spec). Re-running the
  // strip post-mount is idempotent and costs nothing when the render-phase pass won.
  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.has("av_token")) {
      url.searchParams.delete("av_token");
      window.history.replaceState({}, "", url.toString());
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setError(null);
      setShowPrompt(true);
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const token = input.trim();
    if (!token) {
      setError("Enter the access token, or leave it to your administrator to provide.");
      return;
    }
    setStoredApiToken(token);
    // Simplest reliable way for every already-mounted panel (each with its own fetch effect)
    // to immediately retry with the newly-stored token, rather than wiring a refetch callback
    // through every component that calls fetchJSON.
    window.location.reload();
  }

  return (
    <>
      {children}
      {showPrompt && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(5,8,16,0.85)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 1000,
          }}
        >
          <form
            onSubmit={handleSubmit}
            className="card"
            style={{ width: 380, padding: 28 }}
          >
            <div className="card-title" style={{ marginBottom: 4 }}>
              This registry is protected
            </div>
            <p style={{ fontSize: 12.5, color: "var(--text-secondary)", marginBottom: 16 }}>
              Enter the access token to continue. Ask whoever manages this registry for it, or
              run <code style={{ fontSize: 11 }}>av auth status</code> if it&apos;s yours.
            </p>
            <input
              type="password"
              autoFocus
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Access token"
              style={{
                width: "100%",
                background: "var(--bg-deep)",
                border: "1px solid var(--border)",
                color: "var(--text-primary)",
                borderRadius: "var(--radius-sm)",
                padding: "8px 12px",
                fontSize: 13,
                marginBottom: 12,
              }}
            />
            {error && (
              <div className="diff-warning" style={{ marginBottom: 12 }}>
                {error}
              </div>
            )}
            <button type="submit" className="btn btn-primary" style={{ width: "100%" }}>
              Continue
            </button>
          </form>
        </div>
      )}
    </>
  );
}
