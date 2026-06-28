"use client";

import Image from "next/image";

type NavItem = {
  id: string;
  label: string;
  icon: React.ReactNode;
};

const navItems: NavItem[] = [
  {
    id: "dashboard",
    label: "Dashboard",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </svg>
    ),
  },
  {
    id: "commits",
    label: "Commits",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="4" />
        <line x1="1.05" y1="12" x2="7" y2="12" />
        <line x1="17.01" y1="12" x2="22.96" y2="12" />
      </svg>
    ),
  },
  {
    id: "branches",
    label: "Branches",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <line x1="6" y1="3" x2="6" y2="15" />
        <circle cx="18" cy="6" r="3" />
        <circle cx="6" cy="18" r="3" />
        <path d="M18 9a9 9 0 01-9 9" />
      </svg>
    ),
  },
  {
    id: "metrics",
    label: "Metrics",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
      </svg>
    ),
  },
  {
    id: "storage",
    label: "Storage",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="9" ry="3" />
        <path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
        <path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3" />
      </svg>
    ),
  },
  {
    id: "weight-diff",
    label: "Weight Diff",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="8" height="8" rx="1" />
        <rect x="13" y="13" width="8" height="8" rx="1" />
        <path d="M11 7h6a2 2 0 0 1 2 2v4" />
        <path d="M13 17H7a2 2 0 0 1-2-2V11" />
      </svg>
    ),
  },
  {
    id: "projects",
    label: "Projects",
    icon: (
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z" />
      </svg>
    ),
  },
];

interface Props {
  active: string;
  onSelect: (id: string) => void;
}

export function Sidebar({ active, onSelect }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <Image src="/logo.png" alt="Aether-Vault" width={140} height={91} priority />
        <p>ML Registry Dashboard</p>
      </div>

      <nav className="sidebar-nav">
        <div className="nav-section-label">Navigation</div>

        {navItems.map((item) => (
          <button
            key={item.id}
            id={`nav-${item.id}`}
            className={`nav-item ${active === item.id ? "active" : ""}`}
            onClick={() => onSelect(item.id)}
          >
            <span className="nav-icon">{item.icon}</span>
            {item.label}
          </button>
        ))}

        <div className="nav-section-label" style={{ marginTop: 16 }}>System</div>

        <button
          id="nav-docs"
          className="nav-item"
          onClick={() => window.open("http://localhost:8000/docs", "_blank")}
        >
          <span className="nav-icon">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
              <polyline points="10 9 9 9 8 9" />
            </svg>
          </span>
          API Docs
        </button>
      </nav>

      <div style={{
        padding: "16px 20px",
        borderTop: "1px solid var(--border)",
        fontSize: "11px",
        color: "var(--text-muted)",
        lineHeight: 1.6,
      }}>
        <div style={{ fontWeight: 600, color: "var(--text-secondary)", marginBottom: 4 }}>Open Source</div>
        <div>Aether-Vault v1.4.0</div>
        <div>github.com/leon1706</div>
      </div>
    </aside>
  );
}
