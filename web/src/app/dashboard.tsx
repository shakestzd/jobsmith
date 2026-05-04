// dashboard.tsx — port of design/app/dashboard.jsx
//
// Pixel-identical DOM structure and class names. Prop shape derived from how
// main.jsx (design layer) invokes Dashboard:
//   <Dashboard openApp={setOpenSlug} openNew={() => setShowNew(true)} filter="all"/>
// The `filter` prop maps directly to the initial tab selection.

import { useState, useMemo, useEffect } from 'react';
import type { Application } from '../api/types';
import { useApplications } from '../api/hooks';
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

  const query = useApplications();
  const apps: Application[] = query.data ?? [];

  const filtered = useMemo<Application[]>(() => {
    let list = apps;
    if (tab === 'running') list = list.filter(a => a.status === 'running' || a.status === 'queued');
    if (tab === 'review')  list = list.filter(a => a.status === 'review' || a.status === 'draft');
    if (tab === 'rendered') list = list.filter(a => a.status === 'rendered');
    if (q) {
      const needle = q.toLowerCase();
      list = list.filter(a =>
        (a.slug + (a.role ?? '') + (a.company ?? '')).toLowerCase().includes(needle),
      );
    }
    return list;
  }, [apps, tab, q]);

  const renderedCount = apps.filter(a => a.status === 'rendered').length;
  const inProgressCount = apps.filter(a => a.status === 'running' || a.status === 'queued' || a.status === 'draft').length;

  const stats: StatItem[] = [
    { label: 'total', value: apps.length, delta: '+3 this week', up: true },
    { label: 'rendered', value: renderedCount, delta: '67% pass on first render' },
    { label: 'in progress', value: inProgressCount, delta: '1 phase running' },
    { label: 'avg apply time', value: '4m 12s', delta: '−38% vs last month', up: true },
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
            <div
              className={`tab ${tab === 'all' ? 'active' : ''}`}
              onClick={() => setTab('all')}
            >
              all <span className="tab-count">{apps.length}</span>
            </div>
            <div
              className={`tab ${tab === 'running' ? 'active' : ''}`}
              onClick={() => setTab('running')}
            >
              running <span className="tab-count">{apps.filter(a => a.status === 'running' || a.status === 'queued').length}</span>
            </div>
            <div
              className={`tab ${tab === 'review' ? 'active' : ''}`}
              onClick={() => setTab('review')}
            >
              review <span className="tab-count">{apps.filter(a => a.status === 'review' || a.status === 'draft').length}</span>
            </div>
            <div
              className={`tab ${tab === 'rendered' ? 'active' : ''}`}
              onClick={() => setTab('rendered')}
            >
              rendered <span className="tab-count">{renderedCount}</span>
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
            {query.isLoading && (
              <tr>
                <td colSpan={8} style={{ textAlign: 'center', padding: '40px', color: 'var(--fg-subtle)' }}>
                  <span className="mono-sm">loading applications…</span>
                </td>
              </tr>
            )}
            {query.isError && (
              <tr>
                <td colSpan={8} style={{ textAlign: 'center', padding: '40px', color: 'var(--danger, #c43)' }}>
                  <div className="mono-sm" style={{ marginBottom: 8 }}>
                    failed to load applications
                  </div>
                  <button className="btn ghost sm" onClick={() => query.refetch()}>
                    retry
                  </button>
                </td>
              </tr>
            )}
            {query.isSuccess && filtered.map(a => (
              <tr key={a.slug} className="row-clickable" onClick={() => openApp(a.slug)}>
                <td><span className="slug">{a.slug}</span></td>
                <td><span className="role">{a.role ?? '—'}</span></td>
                <td><span className="company">{a.company ?? '—'}</span></td>
                <td><span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{a.phase}/3</span></td>
                <td><span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{a.anchors}</span></td>
                <td><StatusBadge status={a.status} /></td>
                <td><span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>{a.updated}</span></td>
                <td><Icon name="chev" size={12} style={{ opacity: 0.5 }} /></td>
              </tr>
            ))}
            {query.isSuccess && filtered.length === 0 && (
              <tr>
                <td colSpan={8} style={{ textAlign: 'center', padding: '40px', color: 'var(--fg-subtle)' }}>
                  no applications match
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
