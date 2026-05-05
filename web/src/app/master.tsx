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

import { type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { SampleBullet } from '../types';
import { Icon, Badge, SAMPLE_BULLETS } from './shared';
import { SkillForm } from './master/SkillForm';
import { EducationForm } from './master/EducationForm';
import { AuthorForm } from './master/AuthorForm';
import { BenchmarkEditor } from './master/BenchmarkEditor';
import type { Skill, EducationEntry, Author } from './master/schemas';
import { useMasterSection, useMasterSectionWithMeta } from '../api/hooks';
import { JobsmithApiError, apiGet, apiPost, apiPut, formatDetail } from '../api/client';
import type { MasterWorkRole, MasterValidateResponse } from '../api/types';
import {
  apiAuthorToForm,
  apiEducationToForm,
  apiSkillsToForm,
  apiWorkToRoles,
  formToApiSkills,
  formToApiEducation,
  formToApiAuthor,
} from './master/adapters';

// ── Types ────────────────────────────────────────────────────────────────

type MasterTab = 'work' | 'skill' | 'education' | 'author' | 'benchmark';

/** Save state for ETag-backed sections (skill / education / author). */
type SaveState =
  | { kind: 'idle' }
  | { kind: 'saving' }
  | { kind: 'saved' }          // transient 2s pill
  | { kind: 'conflict'; newEtag: string | null }  // 412 — local edits preserved
  | { kind: 'missing'; suggestion: string };      // 404 missing_in_db

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

// ── SaveBar ──────────────────────────────────────────────────────────────

/**
 * Renders the save button + status pills + conflict/missing banners
 * for an ETag-backed tab (Skill, Education, Author).
 */
function SaveBar({
  isDirty,
  saveState,
  onSave,
  onDiscard,
  onOverwrite,
  onCopyCode,
  codeToCopy,
}: {
  isDirty: boolean;
  saveState: SaveState;
  onSave: () => void;
  onDiscard: () => void;
  onOverwrite: () => void;
  onCopyCode: (code: string) => void;
  codeToCopy?: string;
}) {
  const isSaving = saveState.kind === 'saving';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <button
          className="btn primary"
          onClick={onSave}
          disabled={!isDirty || isSaving}
        >
          {isSaving ? 'saving…' : 'save'}
        </button>
        {saveState.kind === 'saved' && (
          <span
            className="pill active"
            role="status"
            style={{ fontSize: 12 }}
          >
            saved
          </span>
        )}
      </div>

      {saveState.kind === 'conflict' && (
        <div
          className="card"
          role="alert"
          style={{ padding: '10px 14px', fontSize: 13, color: 'var(--danger, var(--fg-muted))' }}
        >
          <div style={{ marginBottom: 6 }}>
            section changed elsewhere — refresh to see latest
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn" onClick={onDiscard}>
              discard local + refresh
            </button>
            <button className="btn ghost" onClick={onOverwrite}>
              overwrite anyway
            </button>
          </div>
        </div>
      )}

      {saveState.kind === 'missing' && codeToCopy !== undefined && (
        <div
          className="card"
          role="alert"
          style={{ padding: '10px 14px', fontSize: 13 }}
        >
          <div style={{ marginBottom: 6, color: 'var(--fg-muted)' }}>
            {saveState.suggestion}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <code style={{ fontSize: 12, padding: '2px 6px', background: 'var(--bg-sunk)', borderRadius: 'var(--radius)', flex: 1 }}>
              {codeToCopy}
            </code>
            <button
              className="btn ghost sm"
              onClick={() => onCopyCode(codeToCopy)}
            >
              copy
            </button>
          </div>
        </div>
      )}
    </div>
  );
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
  // The local `toggle(id)` helper that flipped anchor state in setBullets
  // was removed alongside the per-bullet pill controls in feat-aba75dae —
  // the toggle never persisted (no PUT to /api/master/work) so the UI lied
  // about saved state. Anchor flags are now read-only on this overview;
  // the dedicated Mark Anchors page is the entrypoint for editing them.

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
          {/*
            "add bullet" + "save" buttons removed in feat-aba75dae (GH#53).
            Both were decorative — there is no inline-edit/persist flow on
            this page. To add a bullet, edit master/work.yml directly. A
            future feature can re-introduce these once a PUT-section
            round-trip with optimistic locking lands (feat-6999e552).
          */}
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
                {/*
                  Per-bullet pill controls (mark anchor / edit / drop reason…)
                  removed in feat-aba75dae (GH#53). The previous "mark anchor"
                  pill toggled local React state only and did not persist via
                  PUT /api/master/work, which made the UI lie about saved
                  state. To set anchor flags, use the dedicated Mark Anchors
                  page (left sidebar) which writes via the comment-preserving
                  mark-anchors flow.
                */}
                {b.anchor && <span className="pill active">⚑ anchor</span>}
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
            {/*
              "add role" button removed in feat-aba75dae (GH#53). Adding a
              role requires editing master/work.yml directly — there is no
              add-role inline flow. Re-introduce when a section-level
              PUT round-trip exists.
            */}
          </div>

          <BulletEditor role={roles[openIdx] ?? null} />
        </div>
      )}
    </SectionPane>
  );
}

// ── Per-tab wrappers (skill / education / author / benchmark) ────────────

function SkillTab() {
  const { data, etag, isLoading, error, refetch } = useMasterSectionWithMeta('skill');
  const apiSkills = useMemo(() => apiSkillsToForm(data ?? null), [data]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' });
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Hydrate from API on mount and whenever upstream data changes.
  useEffect(() => setSkills(apiSkills), [apiSkills]);

  const isDirty = useMemo(
    () => JSON.stringify(skills) !== JSON.stringify(apiSkills),
    [skills, apiSkills],
  );

  const doSave = useCallback(async (ifMatchOverride?: string | null) => {
    setSaveState({ kind: 'saving' });
    const effectiveEtag = ifMatchOverride !== undefined ? ifMatchOverride : etag;
    try {
      await apiPut('/api/master/skill', formToApiSkills(skills), {
        ifMatch: effectiveEtag ?? undefined,
      });
      refetch();
      setSaveState({ kind: 'saved' });
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
      savedTimerRef.current = setTimeout(() => setSaveState({ kind: 'idle' }), 2000);
    } catch (err) {
      if (err instanceof JobsmithApiError && err.status === 412) {
        // Capture new ETag from 412 response if available (backend may include it).
        setSaveState({ kind: 'conflict', newEtag: null });
      } else if (err instanceof JobsmithApiError && err.status === 404) {
        const suggestion = formatDetail(null, 'jobsmith db load-master  # to backfill skill');
        setSaveState({ kind: 'missing', suggestion });
      } else {
        setSaveState({ kind: 'idle' });
      }
    }
  }, [etag, skills, refetch]);

  const handleDiscard = useCallback(() => {
    setSaveState({ kind: 'idle' });
    refetch();
  }, [refetch]);

  const handleOverwrite = useCallback(() => {
    // Re-PUT with no If-Match (force overwrite).
    void doSave(null);
  }, [doSave]);

  const missing404 = saveState.kind === 'missing';
  const suggestion404 = missing404 ? saveState.suggestion : '';

  return (
    <SectionPane isLoading={isLoading} error={error as Error | null}>
      <SaveBar
        isDirty={isDirty}
        saveState={saveState}
        onSave={() => void doSave()}
        onDiscard={handleDiscard}
        onOverwrite={handleOverwrite}
        onCopyCode={(code) => void navigator.clipboard.writeText(code)}
        codeToCopy={missing404 ? suggestion404 : undefined}
      />
      <SkillForm skills={skills} onChange={setSkills} />
    </SectionPane>
  );
}

function EducationTab() {
  const { data, etag, isLoading, error, refetch } = useMasterSectionWithMeta('education');
  const apiEdu = useMemo(() => apiEducationToForm(data ?? null), [data]);
  const [education, setEducation] = useState<EducationEntry[]>([]);
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' });
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => setEducation(apiEdu), [apiEdu]);

  const isDirty = useMemo(
    () => JSON.stringify(education) !== JSON.stringify(apiEdu),
    [education, apiEdu],
  );

  const doSave = useCallback(async (ifMatchOverride?: string | null) => {
    setSaveState({ kind: 'saving' });
    const effectiveEtag = ifMatchOverride !== undefined ? ifMatchOverride : etag;
    try {
      await apiPut('/api/master/education', formToApiEducation(education), {
        ifMatch: effectiveEtag ?? undefined,
      });
      refetch();
      setSaveState({ kind: 'saved' });
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
      savedTimerRef.current = setTimeout(() => setSaveState({ kind: 'idle' }), 2000);
    } catch (err) {
      if (err instanceof JobsmithApiError && err.status === 412) {
        setSaveState({ kind: 'conflict', newEtag: null });
      } else if (err instanceof JobsmithApiError && err.status === 404) {
        const suggestion = formatDetail(null, 'jobsmith db load-master  # to backfill education');
        setSaveState({ kind: 'missing', suggestion });
      } else {
        setSaveState({ kind: 'idle' });
      }
    }
  }, [etag, education, refetch]);

  const handleDiscard = useCallback(() => {
    setSaveState({ kind: 'idle' });
    refetch();
  }, [refetch]);

  const handleOverwrite = useCallback(() => {
    void doSave(null);
  }, [doSave]);

  const missing404 = saveState.kind === 'missing';
  const suggestion404 = missing404 ? saveState.suggestion : '';

  return (
    <SectionPane isLoading={isLoading} error={error as Error | null}>
      <SaveBar
        isDirty={isDirty}
        saveState={saveState}
        onSave={() => void doSave()}
        onDiscard={handleDiscard}
        onOverwrite={handleOverwrite}
        onCopyCode={(code) => void navigator.clipboard.writeText(code)}
        codeToCopy={missing404 ? suggestion404 : undefined}
      />
      <EducationForm education={education} onChange={setEducation} />
    </SectionPane>
  );
}

function AuthorTab() {
  const { data, etag, isLoading, error, refetch } = useMasterSectionWithMeta('author');
  const apiAuthor = useMemo(() => apiAuthorToForm(data ?? null), [data]);
  // Capture raw API extras for pass-through on save.
  const rawExtras = useMemo<Record<string, unknown>>(() => {
    if (!data) return {};
    // Strip the fields we re-map from author; pass everything else through.
    const { name: _n, email: _e, phone: _p, address: _a, position: _pos,
      profession: _pr, firstname: _f, lastname: _l, contacts: _c, ...rest } = data as Record<string, unknown>;
    return rest;
  }, [data]);

  const [author, setAuthor] = useState<Author>(apiAuthor);
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' });
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => setAuthor(apiAuthor), [apiAuthor]);

  const isDirty = useMemo(
    () => JSON.stringify(author) !== JSON.stringify(apiAuthor),
    [author, apiAuthor],
  );

  const doSave = useCallback(async (ifMatchOverride?: string | null) => {
    setSaveState({ kind: 'saving' });
    const effectiveEtag = ifMatchOverride !== undefined ? ifMatchOverride : etag;
    try {
      await apiPut('/api/master/author', formToApiAuthor(author, rawExtras), {
        ifMatch: effectiveEtag ?? undefined,
      });
      refetch();
      setSaveState({ kind: 'saved' });
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
      savedTimerRef.current = setTimeout(() => setSaveState({ kind: 'idle' }), 2000);
    } catch (err) {
      if (err instanceof JobsmithApiError && err.status === 412) {
        setSaveState({ kind: 'conflict', newEtag: null });
      } else if (err instanceof JobsmithApiError && err.status === 404) {
        const suggestion = formatDetail(null, 'jobsmith db load-master  # to backfill author');
        setSaveState({ kind: 'missing', suggestion });
      } else {
        setSaveState({ kind: 'idle' });
      }
    }
  }, [etag, author, rawExtras, refetch]);

  const handleDiscard = useCallback(() => {
    setSaveState({ kind: 'idle' });
    refetch();
  }, [refetch]);

  const handleOverwrite = useCallback(() => {
    void doSave(null);
  }, [doSave]);

  const missing404 = saveState.kind === 'missing';
  const suggestion404 = missing404 ? saveState.suggestion : '';

  return (
    <SectionPane isLoading={isLoading} error={error as Error | null}>
      <SaveBar
        isDirty={isDirty}
        saveState={saveState}
        onSave={() => void doSave()}
        onDiscard={handleDiscard}
        onOverwrite={handleOverwrite}
        onCopyCode={(code) => void navigator.clipboard.writeText(code)}
        codeToCopy={missing404 ? suggestion404 : undefined}
      />
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
  const [validateState, setValidateState] = useState<
    | { kind: 'idle' }
    | { kind: 'running' }
    | { kind: 'ok' }
    | { kind: 'errors'; errors: { field: string; message: string }[] }
    | { kind: 'failure'; message: string }
  >({ kind: 'idle' });

  const handleValidate = async () => {
    setValidateState({ kind: 'running' });
    try {
      const payload = await apiGet<{
        work: unknown[];
        skill: unknown[];
        education: unknown[];
        author: Record<string, unknown> | null;
      }>('/api/master');
      const result = await apiPost<MasterValidateResponse>('/api/master/validate', {
        work: payload.work,
        skill: payload.skill,
        education: payload.education,
        author: payload.author,
      });
      if (result.ok) {
        setValidateState({ kind: 'ok' });
      } else {
        setValidateState({ kind: 'errors', errors: result.errors });
      }
    } catch (err) {
      setValidateState({
        kind: 'failure',
        message: err instanceof Error ? err.message : String(err),
      });
    }
  };

  const isValidating = validateState.kind === 'running';

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>master content</h1>
          <p>the canonical YAML files every <span className="mono">jobsmith apply</span> draws from. edits here propagate to every future application.</p>
        </div>
        <div className="actions">
          <button className="btn" onClick={handleValidate} disabled={isValidating}>
            <Icon name="doc" size={13} /> {isValidating ? 'validating…' : 'validate'}
          </button>
          {/*
            "open in editor" removed in feat-aba75dae (GH#53). It had no
            handler — the intent was to launch $EDITOR via a vscode://
            URL, but no such launcher exists. Reintroduce when a
            platform-shell launcher (Tauri/Electron) exists.
          */}
        </div>
      </div>

      {validateState.kind === 'ok' && (
        <div
          className="card"
          style={{ padding: '10px 14px', marginBottom: 12, color: 'var(--success, var(--fg-muted))', fontSize: 13 }}
          role="status"
        >
          all sections valid.
        </div>
      )}
      {validateState.kind === 'errors' && (
        <div
          className="card"
          style={{ padding: '10px 14px', marginBottom: 12, fontSize: 13 }}
          role="alert"
        >
          <div style={{ fontWeight: 600, marginBottom: 4 }}>{validateState.errors.length} validation error{validateState.errors.length === 1 ? '' : 's'}</div>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {validateState.errors.map((e, i) => (
              <li key={i}>
                <span className="mono-sm">{e.field}</span>: {e.message}
              </li>
            ))}
          </ul>
        </div>
      )}
      {validateState.kind === 'failure' && (
        <div
          className="card"
          style={{ padding: '10px 14px', marginBottom: 12, color: 'var(--danger, var(--fg-muted))', fontSize: 13 }}
          role="alert"
        >
          validation request failed: {validateState.message}
        </div>
      )}

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
          <span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>
            preview only — run <span className="mono">jobsmith mark-anchors</span> from the CLI to persist
          </span>
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
                {anchorCount} anchors marked in this preview.
              </div>
              {/*
                "save changes" button removed in feat-aba75dae (GH#53). It
                had no handler and no PUT call against /api/master/work.
                Persistence still requires running `jobsmith mark-anchors`
                from the CLI; reintroduce here once a save flow lands.
              */}
              <div style={{ marginTop: 12, fontSize: 12, color: 'var(--fg-subtle)' }}>
                to persist these decisions, run <span className="mono-sm">jobsmith mark-anchors master/work.yml</span>.
              </div>
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
            {/*
              `⌘ S — save & exit` shortcut removed in feat-aba75dae
              (roborev job 946) alongside the decorative "save changes"
              button — handleKeyDown never implemented it, and listing it
              here advertised a save flow the page does not have.
            */}
            {([['A', 'mark as anchor'], ['N', 'non-anchor'], ['S', 'skip for now'], ['↑ ↓', 'navigate']] as [string, string][]).map(([k, v]) => (
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
