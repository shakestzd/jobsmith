// dashboard.tsx — port of design/app/dashboard.jsx, now wired to the live
// /api/applications endpoint via React Query (feat-a6702b30 phase 2).
//
// Pixel-identical DOM structure and class names. Prop shape derived from how
// main.jsx (design layer) invokes Dashboard:
//   <Dashboard openApp={setOpenSlug} openNew={() => setShowNew(true)} filter="all"/>
// The `filter` prop maps directly to the initial tab selection.

import { useState, useMemo, useEffect } from 'react';
import { useApplications } from '../api/hooks';
import { JobsmithApiError } from '../api/client';
import type { ApplicationRow } from '../api/types';
import { Icon, StatusBadge } from './shared';

// ── Prop interfaces ──────────────────────────────────────────────────────────

export interface DashboardProps {
  /** Called when the user clicks a row; receives the application slug. */
  openApp: (slug: string) => void;
  /** Called when the user clicks "new application". */
  openNew: () => void;
  /** Pre-selected tab. Defaults to 'all'. */
  filter?: 'all' | 'running' | 'review' | 'rendered';
}

// ── Internal types ───────────────────────────────────────────────────────────

interface StatItem {
  label: string;
  value: number | string;
  delta: string;
  up?: boolean;
}

/**
 * Decorated row: wraps the raw `ApplicationRow` with cheap derived display
 * fields. Until the API exposes role/company/anchors/factcheck, we synthesise
 * them from slug + timestamps. (TODO feat-?: extend `ApplicationRow` with
 * these fields or a sibling `display` envelope.)
 */
interface DashboardRow {
  slug: string;
  role: string;
  company: string;
  status: string;
  phaseLabel: string;
  anchors: string;
  factcheck: string;
  updated: string;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const RUNNING_STATUSES = new Set(['running', 'queued', 'gather', 'draft']);
const REVIEW_STATUSES = new Set(['review', 'draft']);

function relativeTime(iso: string | null): string {
  if (!iso) return '—';
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return iso;
  const deltaMs = Date.now() - ts;
  const sec = Math.max(0, Math.round(deltaMs / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.round(min / 60);
  if (hr < 48) return `${hr} hr ago`;
  const day = Math.round(hr / 24);
  return `${day}d ago`;
}

function decorate(row: ApplicationRow): DashboardRow {
  const updatedAt = row.finished_at ?? row.started_at;
  return {
    slug: row.slug,
    role: row.role ?? '—',
    company: row.company ?? '—',
    status: row.status,
    phaseLabel: row.phase || '—',
    anchors: '—',
    factcheck: '—',
    updated: relativeTime(updatedAt),
  };
}

// ── Component ────────────────────────────────────────────────────────────────

export function Dashboard({ openApp, openNew, filter = 'all' }: DashboardProps) {
  const [tab, setTab] = useState<'all' | 'running' | 'review' | 'rendered'>(filter);
  // Sidebar nav drives the `filter` prop; sync local tab state when it
  // changes so the table actually filters. Without this, useState(filter)
  // only honors the *initial* value and later sidebar clicks are ignored.
  useEffect(() => {
    setTab(filter);
  }, [filter]);
  const [q, setQ] = useState('');

  const { data: apiApps = [], isLoading, error } = useApplications();

  const rows: DashboardRow[] = useMemo(() => apiApps.map(decorate), [apiApps]);

  const counts = useMemo(() => ({
    all: rows.length,
    running: rows.filter((a) => RUNNING_STATUSES.has(a.status)).length,
    review: rows.filter((a) => REVIEW_STATUSES.has(a.status)).length,
    rendered: rows.filter((a) => a.status === 'rendered').length,
  }), [rows]);

  const filtered = useMemo<DashboardRow[]>(() => {
    let list = rows;
    if (tab === 'running') list = list.filter((a) => RUNNING_STATUSES.has(a.status));
    if (tab === 'review') list = list.filter((a) => REVIEW_STATUSES.has(a.status));
    if (tab === 'rendered') list = list.filter((a) => a.status === 'rendered');
    if (q) {
      const needle = q.toLowerCase();
      list = list.filter((a) => (a.slug + a.role + a.company).toLowerCase().includes(needle));
    }
    return list;
  }, [rows, tab, q]);

  const stats: StatItem[] = [
    { label: 'total', value: counts.all, delta: '' },
    { label: 'rendered', value: counts.rendered, delta: '' },
    { label: 'in progress', value: counts.running, delta: '' },
    { label: 'avg apply time', value: '—', delta: 'TODO: derive from finished/started timestamps' },
  ];

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>applications</h1>
          <p>a tracked record of every <span className="mono">jobsmith apply</span> run, with rendered artifacts and review state.</p>
        </div>
        <div className="actions">
          <button className="btn"><Icon name="folder" size={13} /> import existing</button>
          <button className="btn primary" onClick={openNew}><Icon name="plus" size={13} /> new application</button>
        </div>
      </div>

      <div className="stat-row">
        {stats.map(s => (
          <div key={s.label} className="stat">
            <div className="label">{s.label}</div>
            <div className="value">{s.value}</div>
            <div className={`delta ${s.up ? 'up' : ''}`}>{s.delta}</div>
          </div>
        ))}
      </div>

      <div className="card">
        <div className="card-h">
          <div className="tabs" style={{ borderBottom: 'none', marginBottom: 0 }}>
            <div className={`tab ${tab === 'all' ? 'active' : ''}`} onClick={() => setTab('all')}>
              all <span className="tab-count">{counts.all}</span>
            </div>
            <div className={`tab ${tab === 'running' ? 'active' : ''}`} onClick={() => setTab('running')}>
              running <span className="tab-count">{counts.running}</span>
            </div>
            <div className={`tab ${tab === 'review' ? 'active' : ''}`} onClick={() => setTab('review')}>
              review <span className="tab-count">{counts.review}</span>
            </div>
            <div className={`tab ${tab === 'rendered' ? 'active' : ''}`} onClick={() => setTab('rendered')}>
              rendered <span className="tab-count">{counts.rendered}</span>
            </div>
          </div>
          <div className="right">
            <div className="search" style={{ minWidth: 220 }}>
              <Icon name="search" size={12} />
              <input
                className="grow"
                placeholder="filter by slug, role, company…"
                value={q}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQ(e.target.value)}
                style={{ background: 'transparent', border: 'none', outline: 'none', color: 'var(--fg)', font: 'inherit', width: '100%' }}
              />
            </div>
          </div>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th style={{ width: '30%' }}>slug</th>
              <th>role</th>
              <th>company</th>
              <th>phase</th>
              <th>anchors</th>
              <th>status</th>
              <th>updated</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              [0, 1, 2, 3].map((i) => (
                <tr key={`skeleton-${i}`}>
                  <td colSpan={8} style={{ padding: '14px 16px' }}>
                    <div style={{
                      height: 14,
                      background: 'var(--bg-sunk)',
                      borderRadius: 4,
                      opacity: 0.6 - i * 0.1,
                    }} />
                  </td>
                </tr>
              ))
            )}
            {!isLoading && error && (
              <tr>
                <td colSpan={8} style={{ padding: 32, textAlign: 'center', color: 'var(--fg-muted)' }}>
                  {error instanceof JobsmithApiError && error.status === 401 ? (
                    <div>
                      <div style={{ marginBottom: 8, color: 'var(--danger, #c0392b)' }}>
                        API requires <span className="mono">VITE_JOBSMITH_API_TOKEN</span>.
                      </div>
                      <div className="mono-sm">
                        copy from <code>&lt;project&gt;/private/jobsmith.token</code> to <code>web/.env.local</code>, then restart <code>npm run dev</code>.
                      </div>
                    </div>
                  ) : (
                    <span>failed to load applications: {error.message}</span>
                  )}
                </td>
              </tr>
            )}
            {!isLoading && !error && filtered.length === 0 && rows.length === 0 && (
              <tr>
                <td colSpan={8} style={{ textAlign: 'center', padding: '40px', color: 'var(--fg-subtle)' }}>
                  no applications yet — run <span className="mono">jobsmith apply &lt;jd-url&gt;</span> to create one.
                </td>
              </tr>
            )}
            {!isLoading && !error && filtered.length === 0 && rows.length > 0 && (
              <tr>
                <td colSpan={8} style={{ textAlign: 'center', padding: '40px', color: 'var(--fg-subtle)' }}>
                  no applications match
                </td>
              </tr>
            )}
            {!isLoading && !error && filtered.map((a) => (
              <tr key={a.slug} className="row-clickable" onClick={() => openApp(a.slug)}>
                <td><span className="slug">{a.slug}</span></td>
                <td><span className="role">{a.role}</span></td>
                <td><span className="company">{a.company}</span></td>
                <td><span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{a.phaseLabel}</span></td>
                <td><span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{a.anchors}</span></td>
                <td><StatusBadge status={a.status} /></td>
                <td><span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>{a.updated}</span></td>
                <td><Icon name="chev" size={12} style={{ opacity: 0.5 }} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
