// master.tsx — port of design/app/master.jsx
//
// Exports: MasterContent, MarkAnchorsView
// Internal helpers: WorkEditor, BulletEditor (file-local)
//
// feat-a6702b30 phase 2: each tab now reads from /api/master/<section> via
// `useMasterSection`. Saves remain local-state today (round-trip lossy —
// see feat-6999e552 for ETag/concurrent-write semantics). Forward-only
// adapters live in ./master/adapters.ts; no reverse mapping is attempted
// from form-shape back to API shape in this slice.

import { type KeyboardEvent, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { SampleBullet } from '../types';
import { Icon, Badge, SAMPLE_BULLETS } from './shared';
import { SkillForm } from './master/SkillForm';
import { EducationForm } from './master/EducationForm';
import { AuthorForm } from './master/AuthorForm';
import { BenchmarkEditor } from './master/BenchmarkEditor';
import type { Skill, EducationEntry, Author } from './master/schemas';
import { useMasterSection } from '../api/hooks';
import { JobsmithApiError } from '../api/client';
import type { MasterWorkRole } from '../api/types';
import {
  apiAuthorToForm,
  apiEducationToForm,
  apiSkillsToForm,
  apiWorkToRoles,
} from './master/adapters';

// ── Types ────────────────────────────────────────────────────────────────

type MasterTab = 'work' | 'skill' | 'education' | 'author' | 'benchmark';

// ── Internal helpers ─────────────────────────────────────────────────────

/** Pluralize a count and noun. */
function plural(count: number, singular: string): string {
  return count === 1 ? singular : `${singular}s`;
}

const TABS: Array<[MasterTab, string, string]> = [
  ['work',      'work.yml',      'roles + bullets'],
  ['skill',     'skill.yml',     'skill groups'],
  ['education', 'education.yml', 'entries'],
  ['author',    'author.yml',    'profile'],
  ['benchmark', 'benchmark.md',  'tone reference'],
];

/** Shared loading + error chrome for tabs that wrap a form component. */
function SectionPane({
  isLoading,
  error,
  children,
}: {
  isLoading: boolean;
  error: Error | null;
  children: ReactNode;
}) {
  if (isLoading) {
    return (
      <div className="card" style={{ padding: 24 }}>
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            style={{
              height: 18,
              marginBottom: 10,
              background: 'var(--bg-sunk)',
              borderRadius: 4,
              opacity: 0.6 - i * 0.15,
            }}
          />
        ))}
      </div>
    );
  }
  if (error) {
    return (
      <div className="card" style={{ padding: 24, color: 'var(--fg-muted)' }}>
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
          <span>failed to load section: {error.message}</span>
        )}
      </div>
    );
  }
  return <>{children}</>;
}

// ── BulletEditor ─────────────────────────────────────────────────────────

interface BulletEditorProps {
  role: MasterWorkRole | null;
}

function BulletEditor({ role }: BulletEditorProps) {
  // Convert API bullet shape (string | { bullet, anchor, ... }) into the
  // local SampleBullet shape used by the existing presentational subtree.
  const initial = useMemo<SampleBullet[]>(() => {
    const details = role?.details ?? [];
    return details.map((d, i) => {
      if (typeof d === 'string') {
        return { id: `${role?.title ?? 'role'}-${i}`, anchor: false, text: d };
      }
      return {
        id: `${role?.title ?? 'role'}-${i}`,
        anchor: Boolean(d.anchor),
        text: d.bullet,
      };
    });
  }, [role]);

  const [bullets, setBullets] = useState<SampleBullet[]>(initial);
  // Re-seed when the selected role changes upstream.
  useEffect(() => setBullets(initial), [initial]);

  const anchorCount = bullets.filter(b => b.anchor).length;

  const toggle = (id: string) => {
    setBullets(bs => bs.map(b => b.id === id ? { ...b, anchor: !b.anchor } : b));
  };

  if (!role) {
    return (
      <div className="card" style={{ padding: 24, color: 'var(--fg-muted)' }}>
        no role selected.
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-h">
        <h3>{role.title}{role.location ? ` · ${role.location}` : ''}</h3>
        <span className="sub">{role.date ?? ''} · {bullets.length} {plural(bullets.length, 'bullet')}</span>
        <div className="right">
          <Badge kind="accent">{anchorCount} anchors</Badge>
          <button className="btn ghost sm">add bullet</button>
          {/* save round-trip lossy — see feat-6999e552 for ETag/concurrent-write semantics */}
          <button className="btn ghost sm">save</button>
        </div>
      </div>
      <div style={{ padding: '10px 14px 8px', fontSize: 12, color: 'var(--fg-muted)', borderBottom: '1px solid var(--border)' }}>
        <Icon name="flag" size={11} style={{ verticalAlign: 'middle', color: 'var(--accent)' }} />{' '}
        anchors are bullets <b style={{ color: 'var(--fg)' }}>jobsmith</b> must preserve in every draft (or document a drop-reason).
      </div>
      <div>
        {bullets.map((b, i) => (
          <div key={b.id} className={`bullet-row ${b.anchor ? 'is-anchor' : ''}`}>
            <span className="b-num">{String(i + 1).padStart(2, '0')}</span>
            <div>
              <div className="b-text">{b.text}</div>
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <span className={`pill ${b.anchor ? 'active' : ''}`} onClick={() => toggle(b.id)}>
                  {b.anchor ? '⚑ anchor' : 'mark anchor'}
                </span>
                <span className="pill">edit</span>
                <span className="pill">drop reason…</span>
              </div>
            </div>
            <div className="b-actions">
              <button className="btn ghost sm"><Icon name="chevd" size={11} /></button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── WorkEditor ───────────────────────────────────────────────────────────

function WorkEditor() {
  const { data, isLoading, error } = useMasterSection('work');
  const roles: MasterWorkRole[] = useMemo(() => apiWorkToRoles(data), [data]);
  const [openIdx, setOpenIdx] = useState<number>(0);
  // Reset selection when roles change upstream (e.g., refetch).
  useEffect(() => { if (openIdx >= roles.length) setOpenIdx(0); }, [roles, openIdx]);

  return (
    <SectionPane isLoading={isLoading} error={error as Error | null}>
      {roles.length === 0 ? (
        <div className="card" style={{ padding: 24, color: 'var(--fg-muted)' }}>
          no roles in <span className="mono">master/work.yml</span>.
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
          <div className="card" style={{ padding: 8 }}>
            {roles.map((r, i) => (
              <div
                key={`${r.title}-${i}`}
                onClick={() => setOpenIdx(i)}
                style={{
                  padding: '10px 12px',
                  borderRadius: 'var(--radius)',
                  background: openIdx === i ? 'var(--bg-sunk)' : 'transparent',
                  border: openIdx === i ? '1px solid var(--border)' : '1px solid transparent',
                  cursor: 'pointer',
                  marginBottom: 4,
                }}
              >
                <div style={{ fontWeight: 500, fontSize: 13.5 }}>{r.title}</div>
                <div style={{ fontSize: 12, color: 'var(--fg-muted)' }}>{r.location ?? ''}</div>
                <div className="mono-sm" style={{ color: 'var(--fg-subtle)', marginTop: 2 }}>
                  {(r.date ?? '—')} · {(r.details ?? []).length} {plural((r.details ?? []).length, 'bullet')}
                </div>
              </div>
            ))}
            <div style={{ padding: 8 }}>
              <button className="btn ghost sm" style={{ width: '100%', justifyContent: 'center' }}>
                <Icon name="plus" size={11} /> add role
              </button>
            </div>
          </div>

          <BulletEditor role={roles[openIdx] ?? null} />
        </div>
      )}
    </SectionPane>
  );
}

// ── Per-tab wrappers (skill / education / author / benchmark) ────────────

function SkillTab() {
  const { data, isLoading, error } = useMasterSection('skill');
  const apiSkills = useMemo(() => apiSkillsToForm(data ?? null), [data]);
  const [skills, setSkills] = useState<Skill[]>([]);
  // Hydrate from API on mount and whenever upstream data changes.
  useEffect(() => setSkills(apiSkills), [apiSkills]);
  return (
    <SectionPane isLoading={isLoading} error={error as Error | null}>
      {/* save round-trip lossy — see feat-6999e552 for ETag/concurrent-write semantics */}
      <SkillForm skills={skills} onChange={setSkills} />
    </SectionPane>
  );
}

function EducationTab() {
  const { data, isLoading, error } = useMasterSection('education');
  const apiEdu = useMemo(() => apiEducationToForm(data ?? null), [data]);
  const [education, setEducation] = useState<EducationEntry[]>([]);
  useEffect(() => setEducation(apiEdu), [apiEdu]);
  return (
    <SectionPane isLoading={isLoading} error={error as Error | null}>
      {/* save round-trip lossy — see feat-6999e552 */}
      <EducationForm education={education} onChange={setEducation} />
    </SectionPane>
  );
}

function AuthorTab() {
  const { data, isLoading, error } = useMasterSection('author');
  const apiAuthor = useMemo(() => apiAuthorToForm(data ?? null), [data]);
  const [author, setAuthor] = useState<Author>(apiAuthor);
  useEffect(() => setAuthor(apiAuthor), [apiAuthor]);
  return (
    <SectionPane isLoading={isLoading} error={error as Error | null}>
      {/* save round-trip lossy — see feat-6999e552 */}
      <AuthorForm author={author} onChange={setAuthor} />
    </SectionPane>
  );
}

function BenchmarkTab() {
  const { data, isLoading, error } = useMasterSection('benchmark');
  const initial = data?.text ?? '';
  const [text, setText] = useState<string>(initial);
  useEffect(() => setText(initial), [initial]);
  return (
    <SectionPane isLoading={isLoading} error={error as Error | null}>
      {/* save round-trip lossy — see feat-6999e552 */}
      <BenchmarkEditor text={text} onChange={setText} />
    </SectionPane>
  );
}

// ── Exported components ──────────────────────────────────────────────────

export function MasterContent() {
  const [tab, setTab] = useState<MasterTab>('work');

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>master content</h1>
          <p>the canonical YAML files every <span className="mono">jobsmith apply</span> draws from. edits here propagate to every future application.</p>
        </div>
        <div className="actions">
          <button className="btn"><Icon name="doc" size={13} /> validate</button>
          <button className="btn"><Icon name="folder" size={13} /> open in editor</button>
        </div>
      </div>

      <div className="tabs">
        {TABS.map(([id, label, sub]) => (
          <div
            key={id}
            className={`tab ${tab === id ? 'active' : ''}`}
            onClick={() => setTab(id)}
          >
            {label} <span className="tab-count">{sub}</span>
          </div>
        ))}
      </div>

      {tab === 'work' && <WorkEditor />}
      {tab === 'skill' && <SkillTab />}
      {tab === 'education' && <EducationTab />}
      {tab === 'author' && <AuthorTab />}
      {tab === 'benchmark' && <BenchmarkTab />}
    </div>
  );
}

// ── MarkAnchorsView ──────────────────────────────────────────────────────

interface BulletWithDecision extends SampleBullet {
  decided: boolean;
  decision: 'anchor' | 'non-anchor' | 'skip' | null;
}

type Decision = 'anchor' | 'non-anchor' | 'skip';

export function MarkAnchorsView() {
  const [bullets, setBullets] = useState<BulletWithDecision[]>(
    SAMPLE_BULLETS.map(b => ({
      ...b,
      decided: false,
      decision: b.anchor ? 'anchor' : null,
    }))
  );
  const [idx, setIdx] = useState<number>(() =>
    SAMPLE_BULLETS.findIndex(b => !b.anchor)
  );

  const cur = bullets[idx >= 0 ? idx : 0];
  const done = bullets.every(b => b.decided);
  const anchorCount = bullets.filter(b => b.decision === 'anchor').length;

  const decide = (decision: Decision) => {
    setBullets(bs =>
      bs.map((b, i) =>
        i === idx ? { ...b, decided: true, decision, anchor: decision === 'anchor' } : b
      )
    );
    const next = bullets.findIndex((b, i) => i > idx && !b.decided);
    setIdx(next === -1 ? bullets.length : next);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'a' || e.key === 'A') decide('anchor');
    else if (e.key === 'n' || e.key === 'N') decide('non-anchor');
    else if (e.key === 's' || e.key === 'S') decide('skip');
    else if (e.key === 'ArrowUp') setIdx(i => Math.max(0, i - 1));
    else if (e.key === 'ArrowDown') setIdx(i => Math.min(bullets.length - 1, i + 1));
  };

  return (
    <div className="content" onKeyDown={handleKeyDown} tabIndex={0}>
      <div className="page-head">
        <div>
          <h1>mark anchors</h1>
          <p>walk every bullet in <span className="mono">master/work.yml</span> and tag the ones that must survive every draft.</p>
        </div>
        <div className="actions">
          <span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>round-trip via ruamel.yaml — comments preserved</span>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: 16 }}>
        <div className="card">
          <div className="card-h">
            <h3>Senior Engineer · Recurly Engineering</h3>
            <span className="sub">{bullets.filter(b => b.decided).length} / {bullets.length} reviewed</span>
            <div className="right"><Badge kind="accent">{anchorCount} anchors</Badge></div>
          </div>

          {!done ? (
            <div style={{ padding: '24px 28px' }}>
              <div className="mono-sm" style={{ color: 'var(--fg-subtle)', marginBottom: 8 }}>BULLET {idx + 1} OF {bullets.length}</div>
              <div style={{ fontSize: 16, lineHeight: 1.5, marginBottom: 24, fontWeight: 400 }}>
                {cur && cur.text}
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button className="btn primary" onClick={() => decide('anchor')}>
                  <Icon name="flag" size={12} /> anchor <span className="kbd" style={{ marginLeft: 4, background: 'oklch(1 0 0 / 0.15)', borderColor: 'transparent', color: 'inherit' }}>A</span>
                </button>
                <button className="btn" onClick={() => decide('non-anchor')}>
                  non-anchor <span className="kbd" style={{ marginLeft: 4 }}>N</span>
                </button>
                <button className="btn ghost" onClick={() => decide('skip')}>
                  skip <span className="kbd" style={{ marginLeft: 4 }}>S</span>
                </button>
                <div style={{ marginLeft: 'auto', color: 'var(--fg-subtle)', fontSize: 12, alignSelf: 'center' }}>
                  <span className="kbd">↑</span> previous &nbsp; <span className="kbd">↓</span> next
                </div>
              </div>
            </div>
          ) : (
            <div style={{ padding: '40px 28px', textAlign: 'center' }}>
              <Icon name="check" size={32} style={{ color: 'var(--success)' }} />
              <div style={{ fontSize: 16, marginTop: 10 }}>all bullets reviewed</div>
              <div style={{ color: 'var(--fg-muted)', marginTop: 4, fontSize: 13 }}>
                {anchorCount} anchors marked · changes ready to write back to work.yml
              </div>
              <button className="btn primary" style={{ marginTop: 18 }}><Icon name="check" size={12} /> save changes</button>
            </div>
          )}

          <div style={{ borderTop: '1px solid var(--border)' }}>
            <div style={{ padding: '10px 16px', fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>queue</div>
            {bullets.map((b, i) => (
              <div
                key={b.id}
                onClick={() => setIdx(i)}
                style={{
                  padding: '8px 16px',
                  display: 'grid',
                  gridTemplateColumns: '24px 1fr auto',
                  gap: 10,
                  cursor: 'pointer',
                  background: idx === i ? 'var(--bg-sunk)' : 'transparent',
                  borderLeft: idx === i ? '2px solid var(--accent)' : '2px solid transparent',
                  borderBottom: '1px solid var(--border)',
                }}
              >
                <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>{String(i + 1).padStart(2, '0')}</span>
                <span style={{ fontSize: 13, color: 'var(--fg-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b.text}</span>
                <span>
                  {b.decision === 'anchor'     && <Badge kind="accent">anchor</Badge>}
                  {b.decision === 'non-anchor' && <Badge>non-anchor</Badge>}
                  {b.decision === 'skip'       && <Badge kind="warn">skip</Badge>}
                  {!b.decided                  && <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>—</span>}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="card" style={{ height: 'fit-content' }}>
          <div className="card-h"><h3>shortcuts</h3></div>
          <div style={{ padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {([['A', 'mark as anchor'], ['N', 'non-anchor'], ['S', 'skip for now'], ['↑ ↓', 'navigate'], ['⌘ S', 'save & exit']] as [string, string][]).map(([k, v]) => (
              <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 12.5 }}>
                <span className="kbd" style={{ minWidth: 36, textAlign: 'center' }}>{k}</span>
                <span style={{ color: 'var(--fg-muted)' }}>{v}</span>
              </div>
            ))}
          </div>
          <div style={{ borderTop: '1px solid var(--border)', padding: '12px 16px', background: 'var(--bg-sunk)', fontSize: 12, color: 'var(--fg-muted)' }}>
            equivalent to: <span className="mono-sm" style={{ color: 'var(--fg)' }}>jobsmith mark-anchors master/work.yml</span>
          </div>
        </div>
      </div>
    </div>
  );
}
