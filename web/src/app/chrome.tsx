// chrome.tsx — Sidebar + Topbar components for the jobsmith shell.

import { Fragment, useEffect, useState } from 'react';
import { Icon } from './shared';
import { useApplications } from '../api/hooks';
import type { IconName, ThemeName, ViewName } from '../types';

// ── Postings badge (new-since-last-visit) ─────────────────────────────────
const LAST_VISIT_KEY = 'jobsmith.postings.last_visit';

function usePostingsBadge(): number {
  const [count, setCount] = useState(0);

  useEffect(() => {
    // Compute badge by fetching sourced postings newer than last visit.
    // We re-read from localStorage each time this hook runs so the badge
    // reflects the count at the time the user OPENS the inbox (not now).
    import('../api/client').then(({ getPostings }) => {
      getPostings({ status: 'sourced' })
        .then((postings) => {
          const raw = localStorage.getItem(LAST_VISIT_KEY);
          const lv = raw ? Date.parse(raw) : 0;
          setCount(postings.filter((p) => Date.parse(p.first_seen_at) > lv).length);
        })
        .catch(() => setCount(0));
    });
  }, []);

  return count;
}

// ── Sidebar ──────────────────────────────────────────────────────────────
export interface SidebarProps {
  view: ViewName;
  open: boolean;
  setView: (next: ViewName) => void;
  openNew: () => void;
}

interface NavItemProps {
  id: ViewName;
  icon: IconName;
  label: string;
  count?: number;
  view: ViewName;
  setView: (next: ViewName) => void;
}

function NavItem({ id, icon, label, count, view, setView }: NavItemProps) {
  return (
    <div
      className={`nav-item ${view === id ? 'active' : ''}`}
      onClick={() => setView(id)}
    >
      <Icon name={icon} size={14} />
      <span>{label}</span>
      {count != null && <span className="nav-count">{count}</span>}
    </div>
  );
}

export function Sidebar({ view, open, setView, openNew }: SidebarProps) {
  const { data: apps } = useApplications();
  const postingsBadge = usePostingsBadge();

  const counts = apps
    ? {
        dashboard: apps.length,
        running: apps.filter((a) => a.ui_phase === 'running').length,
        review: apps.filter((a) => a.ui_phase === 'review').length,
      }
    : undefined;

  return (
    <aside className="sidebar" style={{ overflow: 'hidden', opacity: open ? 1 : 0 }}>
      <div className="brand">
        <span className="brand-mark"></span>
        <span>jobsmith</span>
      </div>

      <button
        className="btn primary"
        style={{ justifyContent: 'flex-start', marginBottom: 14 }}
        onClick={openNew}
      >
        <Icon name="plus" size={13} /> new application
        <span style={{ marginLeft: 'auto', fontFamily: 'var(--font-mono)', fontSize: 11, opacity: 0.7 }}>
          ⌘N
        </span>
      </button>

      <NavItem id="dashboard" icon="home" label="Applications" count={counts?.dashboard} view={view} setView={setView} />
      <NavItem id="running" icon="bolt" label="In progress" count={counts?.running} view={view} setView={setView} />
      <NavItem id="review" icon="eye" label="Needs review" count={counts?.review} view={view} setView={setView} />
      <NavItem id="postings" icon="inbox" label="Postings" count={postingsBadge > 0 ? postingsBadge : undefined} view={view} setView={setView} />
      <NavItem id="funnel" icon="chart" label="Funnel" view={view} setView={setView} />

      <div className="nav-section">Authoring</div>
      <NavItem id="master" icon="yaml" label="Master content" view={view} setView={setView} />
      <NavItem id="anchors" icon="flag" label="Mark anchors" view={view} setView={setView} />
      <NavItem id="site" icon="site" label="Listings site" view={view} setView={setView} />
      <NavItem id="feedback" icon="msg" label="Feedback" view={view} setView={setView} />

      <div className="nav-section">System</div>
      <NavItem id="onboard" icon="user" label="Onboarding" view={view} setView={setView} />
      <NavItem id="doctor" icon="cog" label="Doctor" view={view} setView={setView} />
      <NavItem id="config" icon="doc" label="Config" view={view} setView={setView} />

      <div className="sidebar-footer">
        <span className="dot"></span>
        <span>cli v0.4.1 · ok</span>
      </div>
    </aside>
  );
}

// ── Topbar ───────────────────────────────────────────────────────────────
export interface TopbarProps {
  crumbs: string[];
  onSearch: () => void;
  onTheme: () => void;
  onToggleChat: () => void;
  onToggleSidebar: () => void;
  sidebarOpen: boolean;
  theme: ThemeName;
}

export function Topbar({ crumbs, onSearch, onTheme, onToggleChat, onToggleSidebar, sidebarOpen, theme }: TopbarProps) {
  return (
    <div className="topbar">
      {/* Sidebar toggle — chevron rotates to indicate collapsed state */}
      <button
        className="btn ghost sm"
        onClick={onToggleSidebar}
        title={sidebarOpen ? 'hide sidebar' : 'show sidebar'}
        style={{ flexShrink: 0 }}
      >
        <Icon
          name="chev"
          size={13}
          style={{ transform: sidebarOpen ? 'rotate(180deg)' : 'none', transition: 'transform 150ms' }}
        />
      </button>

      <div className="crumbs">
        {crumbs.map((c, i) => (
          <Fragment key={i}>
            {i > 0 && <span className="sep">/</span>}
            <span className={i === crumbs.length - 1 ? 'here' : ''}>{c}</span>
          </Fragment>
        ))}
      </div>

      <div className="topbar-right">
        <div className="search" onClick={onSearch}>
          <Icon name="search" size={13} />
          <span className="grow">jump to slug, command, file…</span>
          <span className="kbd">⌘K</span>
        </div>
        <button className="btn ghost sm" onClick={onToggleChat} title="chat">
          <Icon name="msg" size={13} />
        </button>
        <button className="btn ghost sm" onClick={onTheme} title={`theme: ${theme}`}>
          <Icon name="sun" size={13} />
        </button>
        <div
          style={{
            width: 28, height: 28, borderRadius: '50%',
            background: 'linear-gradient(135deg, oklch(0.65 0.16 268), oklch(0.5 0.18 295))',
            display: 'grid', placeItems: 'center',
            color: 'white', fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 600,
          }}
        >
          js
        </div>
      </div>
    </div>
  );
}
