// application.tsx — port of design/app/application.jsx
//
// Exports: ApplicationDetail (top-level).
// Sub-components (PhaseCard, PipelineTab, ArtifactsTab, PdfPreview,
// FactCheckTab, AnchorCheckTab, ConfigTab) are file-local.
//
// Live data flow (slice 6):
//   useApplication(slug) -> ApplicationDetail
//     extends Application with `artifacts`, `prose_draft` (truncated to
//     64 KB by the backend), `cover_letter_draft`, `fact_check`,
//     `anchor_check`, `bullet_selection`, `variables`, `config`, and
//     `truncated: bool`. The `truncated` flag is plumbed into ArtifactsTab
//     so the user can fetch the full file via /api/applications/{slug}/raw/.
//
// Slice 8 (SSE) will replace the seeded NEW_EVENTS sim in PipelineTab with
// a live `useEventStream(slug)` call from `web/src/api/events.ts`.

import { useState, useEffect, useMemo, useRef } from 'react';
import { useApplication, useRerunApplication } from '../api/hooks';
import { ApiError } from '../api/client';
import {
  useEventStream,
  type PipelineEvent,
  type Verbosity,
  type ConnectionStatus,
} from '../api/events';
import type {
  Application,
  ApplicationDetail as TApplicationDetail,
  RerunConflictResponse,
} from '../api/types';
import type { IconName } from '../types';
import { Icon, Badge, StatusBadge } from './shared';

// ── Public prop type ─────────────────────────────────────────────────────────

export interface ApplicationDetailProps {
  /** The application slug to display. */
  slug: string;
  /** Navigate back to the applications list. */
  back: () => void;
}

// ── Phase-related helpers ────────────────────────────────────────────────────

type PhaseStatus = 'done' | 'running' | 'queued';

interface PhaseSpec {
  num: 1 | 2 | 3;
  name: string;
  blurb: string;
  specs: string[];
}

const PHASES: PhaseSpec[] = [
  {
    num: 1,
    name: 'gather',
    blurb: 'parse JD, score anchors, build spec.json',
    specs: ['apply-jd-parser', 'apply-anchor-scorer', 'apply-spec-builder'],
  },
  {
    num: 2,
    name: 'draft',
    blurb: 'select bullets, draft cover, fact-check',
    specs: ['apply-bullet-selector', 'apply-cover-drafter', 'apply-factchecker'],
  },
  {
    num: 3,
    name: 'render',
    blurb: 'assemble _variables.yml, quarto render',
    specs: ['apply-assembler', 'apply-renderer'],
  },
];

// ── Event stream helpers ─────────────────────────────────────────────────────

interface LogEvent {
  ts: string;
  lvl: string;
  msg: string;
}

function now(): string {
  return new Date().toTimeString().slice(0, 8);
}

function phaseDuration(n: number): string {
  return (['1.4s', '3.8s', '12.1s'] as const)[n - 1] ?? '—';
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/** Format an ISO-ish timestamp as HH:MM:SS, falling back to "—" on garbage. */
function formatTs(input: string | null | undefined): string {
  if (!input) return now();
  const d = new Date(input);
  if (Number.isNaN(d.getTime())) return now();
  return d.toTimeString().slice(0, 8);
}

/** Convert a live SSE PipelineEvent into the row shape PipelineTab renders. */
function pipelineEventToLog(evt: PipelineEvent): LogEvent {
  if (evt.kind === 'phase') {
    const ts = formatTs(evt.data.finished_at ?? evt.data.started_at ?? evt.receivedAt);
    return {
      ts,
      lvl: 'info',
      msg:
        '<span class="dim">phase=</span>' +
        escapeHtml(evt.data.phase) +
        ' <span class="dim">status=</span>' +
        escapeHtml(evt.data.status),
    };
  }
  const ts = formatTs(evt.data.finished_at ?? evt.receivedAt);
  return {
    ts,
    lvl: 'spec',
    msg:
      escapeHtml(evt.data.specialist) +
      ': <span class="dim">kind=</span>' +
      escapeHtml(evt.data.kind),
  };
}

// ── Tab type ─────────────────────────────────────────────────────────────────

type TabName = 'pipeline' | 'artifacts' | 'factcheck' | 'anchors' | 'config';

// ── Progress map type ────────────────────────────────────────────────────────

type ProgressMap = Record<1 | 2 | 3, number>;

// ── Progress derivation helper ───────────────────────────────────────────────

function deriveProgress(app: Application): {
  progress: ProgressMap;
  activePhase: 1 | 2 | 3;
} {
  // If rendered or done, all phases complete
  if (app.status === 'rendered' || app.status === 'done') {
    return {
      progress: { 1: 100, 2: 100, 3: 100 },
      activePhase: 3,
    };
  }

  // For failed status, initialize based on phase but don't fake completion
  if (app.status === 'failed') {
    const failedPhase = ((app.phase || 1) as 1 | 2 | 3);
    const progress: ProgressMap = { 1: 0, 2: 0, 3: 0 };
    if (failedPhase >= 1) progress[1] = 100;
    if (failedPhase >= 2) progress[2] = 100;
    if (failedPhase >= 3) progress[3] = 100;
    return { progress, activePhase: failedPhase };
  }

  // For running statuses: initialize based on phase
  if (app.phase === 0) {
    return {
      progress: { 1: 0, 2: 0, 3: 0 },
      activePhase: 1,
    };
  }

  if (app.phase === 1 || app.status === 'gather') {
    return {
      progress: { 1: app.status === 'running' ? 42 : 0, 2: 0, 3: 0 },
      activePhase: 1,
    };
  }

  if (app.phase === 2 || app.status === 'draft') {
    return {
      progress: { 1: 100, 2: app.status === 'running' ? 42 : 0, 3: 0 },
      activePhase: 2,
    };
  }

  if (app.phase === 3 || app.status === 'review') {
    return {
      progress: { 1: 100, 2: 100, 3: app.status === 'running' ? 42 : 0 },
      activePhase: 3,
    };
  }

  return {
    progress: { 1: 0, 2: 0, 3: 0 },
    activePhase: 1,
  };
}

// ── PhaseCard ────────────────────────────────────────────────────────────────

interface PhaseMetaEntry {
  v: string | number;
  k: string;
}

interface PhaseCardProps {
  num: number;
  name: string;
  blurb: string;
  status: PhaseStatus;
  progress: number;
  onClick: () => void;
  active: boolean;
  meta: PhaseMetaEntry[];
}

function PhaseCard({ num, name, blurb, status, progress, onClick, active, meta }: PhaseCardProps) {
  return (
    <div className={`phase ${active ? 'active' : ''}`} onClick={onClick}>
      <div className="phase-head">
        <span className="phase-num">PHASE {num}</span>
        <span className="phase-name">{name}</span>
        <span className="phase-status">
          {status === 'running' && <><span className="spin" /> running</>}
          {status === 'done' && <><Icon name="check" size={12} className="check" style={{ color: 'var(--success)' }} /> done</>}
          {status === 'queued' && <>queued</>}
        </span>
      </div>
      <div style={{ fontSize: 12.5, color: 'var(--fg-muted)', marginBottom: 8 }}>{blurb}</div>
      <div className="phase-bar">
        <div className={`phase-bar-fill ${status === 'done' ? 'done' : ''}`} style={{ width: `${progress}%` }} />
      </div>
      <div className="phase-meta">
        {meta.map((m, i) => <span key={i}><b>{m.v}</b> {m.k}</span>)}
      </div>
    </div>
  );
}

// ── PipelineTab ──────────────────────────────────────────────────────────────

interface PipelineTabProps {
  slug: string;
  running: boolean;
  phase: number;
  progress: ProgressMap;
}

const STATUS_DOT_COLOR: Record<ConnectionStatus, string> = {
  open: 'var(--success)',
  connecting: 'var(--accent, #d49a3a)',
  closed: 'var(--fg-subtle)',
  error: 'var(--danger, #c43)',
};

function PipelineTab({ slug, running, phase, progress }: PipelineTabProps) {
  const logRef = useRef<HTMLDivElement>(null);
  const [verbosity, setVerbosity] = useState<Verbosity>('normal');

  // feat-440324f1: live SSE event stream replaces the previous mock seed.
  const { events: live, status: streamStatus } = useEventStream(slug, {
    verbosity,
  });

  const events = useMemo<LogEvent[]>(
    () => live.map(pipelineEventToLog),
    [live],
  );

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [events]);

  const phaseSpec = PHASES[phase - 1];
  const phaseNum = phaseSpec.num;
  const phaseDone = progress[phaseNum] >= 100;

  const hasEvents = events.length > 0;
  const showEmpty = !hasEvents && (streamStatus === 'open' || streamStatus === 'connecting');

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 360px', gap: 16 }}>
      <div className="card">
        <div className="card-h">
          <h3>event stream</h3>
          <span className="sub">phase {phase} · {events.length} events</span>
          <span
            title={`stream ${streamStatus}`}
            aria-label={`stream ${streamStatus}`}
            style={{
              display: 'inline-block',
              width: 8,
              height: 8,
              marginLeft: 8,
              borderRadius: '50%',
              background: STATUS_DOT_COLOR[streamStatus],
            }}
          />
          <div className="right">
            <button
              className="btn ghost sm"
              aria-pressed={verbosity === 'quiet'}
              style={verbosity === 'quiet' ? { borderColor: 'var(--border)', background: 'var(--bg-sunk)' } : undefined}
              onClick={() => setVerbosity('quiet')}
            >−v</button>
            <button
              className="btn ghost sm"
              aria-pressed={verbosity === 'normal'}
              style={verbosity === 'normal' ? { borderColor: 'var(--border)', background: 'var(--bg-sunk)' } : undefined}
              onClick={() => setVerbosity('normal')}
            >−vv</button>
            <button
              className="btn ghost sm"
              aria-pressed={verbosity === 'verbose'}
              style={verbosity === 'verbose' ? { borderColor: 'var(--border)', background: 'var(--bg-sunk)' } : undefined}
              onClick={() => setVerbosity('verbose')}
            >−vvv</button>
            <button className="btn ghost sm">copy</button>
          </div>
        </div>
        <div className="eventlog" ref={logRef} style={{ maxHeight: 460, borderRadius: 0, border: 'none' }}>
          {showEmpty && (
            <div style={{ padding: '24px 14px', color: 'var(--fg-subtle)', fontSize: 12 }}>
              <span className="mono-sm">no events yet</span>
              <span style={{ marginLeft: 6 }}>— start a run to see live activity</span>
            </div>
          )}
          {events.map((e, i) => (
            <div key={i}>
              <span className="ts">{e.ts}</span>
              <span className={`lvl ${e.lvl}`}>{e.lvl.padEnd(6)}</span>
              <span className="msg" dangerouslySetInnerHTML={{ __html: e.msg }} />
            </div>
          ))}
          {running && streamStatus === 'open' && (
            <div>
              <span className="ts">{now()}</span>
              <span className="lvl info">stream</span>
              <span className="dim">▍</span>
            </div>
          )}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div className="card">
          <div className="card-h">
            <h3>specialists</h3>
            <span className="sub">phase {phase}</span>
          </div>
          <div style={{ padding: '8px 4px' }}>
            {phaseSpec.specs.map(s => {
              const pct = phaseDone ? 100 : Math.min(100, progress[phaseNum] * (1 + Math.random() * 0.4));
              void pct;
              const iconName: IconName = phaseDone ? 'check' : 'dot';
              return (
                <div key={s} style={{ padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 10 }}>
                  <Icon
                    name={iconName}
                    size={12}
                    style={{ color: phaseDone ? 'var(--success)' : 'var(--accent)' }}
                  />
                  <span className="mono-sm" style={{ flex: 1 }}>{s}</span>
                  <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>
                    {phaseDone
                      ? `${(Math.random() * 1.5 + 0.4).toFixed(1)}s`
                      : (progress[phaseNum] > 0 ? 'running' : 'queued')}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        <div className="card">
          <div className="card-h">
            <h3>db writes</h3>
            <span className="sub">private/jobsmith.db</span>
          </div>
          <div style={{ padding: '12px 16px', fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--fg-muted)', lineHeight: 1.9 }}>
            <div><b style={{ color: 'var(--fg)' }}>apply_runs</b>     <span style={{ color: 'var(--fg-subtle)' }}>1 row</span></div>
            <div><b style={{ color: 'var(--fg)' }}>spec</b>           <span style={{ color: 'var(--fg-subtle)' }}>1 row</span></div>
            <div><b style={{ color: 'var(--fg)' }}>bullet_selection</b><span style={{ color: 'var(--fg-subtle)', marginLeft: 6 }}>14 rows</span></div>
            <div><b style={{ color: 'var(--fg)' }}>cover_draft</b>    <span style={{ color: 'var(--fg-subtle)' }}>1 row</span></div>
            <div><b style={{ color: 'var(--fg)' }}>renders</b>        <span style={{ color: 'var(--fg-subtle)' }}>2 rows</span></div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── ArtifactsTab ─────────────────────────────────────────────────────────────

interface ArtifactsTabProps {
  detail: TApplicationDetail;
}

interface RenderableFile {
  /** Filename used as the selection key + the /raw/ allowlist key. */
  name: string;
  /** Display name (matches the design's tree). */
  label: string;
  /** Display size string ("2.1 KB", "—"). */
  size: string;
  /** Pre-loaded preview body, or null if we have no body for this file. */
  body: string | null;
  /** True when the body field was server-truncated. */
  truncated: boolean;
}

function fmtBytes(n: number | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function jsonOrEmpty(value: Record<string, unknown> | null | undefined): string {
  if (!value) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '';
  }
}

function ArtifactsTab({ detail }: ArtifactsTabProps) {
  // Resolve display sizes from the artifact tree (best effort — fall back to
  // a string sentinel when the file is not represented).
  const sizeFor = (name: string): string => {
    const all = [...(detail.artifacts?.apply_state ?? []), ...(detail.artifacts?.rendered ?? [])];
    const node = all.find(n => n.name === name);
    return node ? fmtBytes(node.size) : '—';
  };

  // Map our preview catalogue to the live payload. `truncated` covers prose +
  // cover-letter together (matches the backend's single boolean flag).
  const proseTruncated = detail.truncated && Boolean(detail.prose_draft);
  const coverTruncated = detail.truncated && Boolean(detail.cover_letter_draft);

  const files: RenderableFile[] = [
    { name: 'jd-parsed.json',         label: 'jd-parsed.json',         size: sizeFor('jd-parsed.json'),         body: jsonOrEmpty(detail.spec),             truncated: false },
    { name: 'bullet_selection.json',  label: 'bullet_selection.json',  size: sizeFor('bullet_selection.json'),  body: jsonOrEmpty(detail.bullet_selection), truncated: false },
    { name: 'prose-draft.md',         label: 'prose-draft.md',         size: sizeFor('prose-draft.md'),         body: detail.prose_draft ?? '',             truncated: proseTruncated },
    { name: 'cover-letter-draft.md',  label: 'cover-letter-draft.md',  size: sizeFor('cover-letter-draft.md'),  body: detail.cover_letter_draft ?? '',      truncated: coverTruncated },
    { name: 'fact_check.json',        label: 'fact_check.json',        size: sizeFor('fact_check.json'),        body: jsonOrEmpty(detail.fact_check),       truncated: false },
    { name: 'anchor_check.json',      label: 'anchor_check.json',      size: sizeFor('anchor_check.json'),      body: jsonOrEmpty(detail.anchor_check),     truncated: false },
    { name: '_variables.yml',         label: '_variables.yml',         size: sizeFor('_variables.yml'),         body: jsonOrEmpty(detail.variables),        truncated: false },
  ];

  // Add rendered PDFs (pdf preview is a placeholder in the design)
  const rendered = detail.artifacts?.rendered ?? [];
  for (const r of rendered) {
    if (r.name.endsWith('.pdf')) {
      files.push({ name: r.name, label: r.name, size: fmtBytes(r.size), body: '__PDF_PREVIEW__', truncated: false });
    }
  }

  // Default selection: prose-draft.md if it has a body, else first non-empty.
  const initialSel = files.find(f => f.name === 'prose-draft.md' && f.body)?.name
    ?? files.find(f => f.body && f.body !== '__PDF_PREVIEW__')?.name
    ?? files[0]?.name
    ?? 'prose-draft.md';
  const [sel, setSel] = useState<string>(initialSel);

  const current = files.find(f => f.name === sel) ?? files[0];

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
      <div className="card" style={{ padding: '12px' }}>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', padding: '4px 8px 8px' }}>artifacts</div>
        <div className="tree">
          <div>
            <div className="tree-row">
              <Icon name="chevd" size={10} className="caret" />
              <Icon name="folder" size={12} className="ico" />
              <span style={{ color: 'var(--fg)' }}>.apply-state/</span>
            </div>
            <div className="tree-children">
              {files.filter(f => f.name.endsWith('.json') || f.name.endsWith('.md')).map(f => (
                <div
                  key={f.name}
                  className={`tree-row ${sel === f.name ? 'active' : ''}`}
                  onClick={() => setSel(f.name)}
                >
                  <span className="caret" />
                  <Icon name="doc" size={11} className="ico" />
                  <span style={{ flex: 1 }}>{f.label}</span>
                  <span style={{ color: 'var(--fg-subtle)', fontSize: 10.5 }}>{f.size}</span>
                </div>
              ))}
            </div>
          </div>
          <div>
            <div className="tree-row">
              <Icon name="chevd" size={10} className="caret" />
              <Icon name="folder" size={12} className="ico" />
              <span style={{ color: 'var(--fg)' }}>rendered/</span>
            </div>
            <div className="tree-children">
              {files.filter(f => f.name.endsWith('.yml') || f.name.endsWith('.pdf')).map(f => (
                <div
                  key={f.name}
                  className={`tree-row ${sel === f.name ? 'active' : ''}`}
                  onClick={() => setSel(f.name)}
                >
                  <span className="caret" />
                  <Icon name="doc" size={11} className="ico" />
                  <span style={{ flex: 1 }}>{f.label}</span>
                  <span style={{ color: 'var(--fg-subtle)', fontSize: 10.5 }}>{f.size}</span>
                </div>
              ))}
              {rendered.length === 0 && (
                <div style={{ padding: '6px 12px', fontSize: 11, color: 'var(--fg-subtle)' }}>
                  <span className="mono-sm">no renders yet</span>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-h">
          <Icon name="doc" size={13} />
          <h3 style={{ fontFamily: 'var(--font-mono)', fontWeight: 500 }}>{current?.label ?? sel}</h3>
          <div className="right">
            {current?.truncated && (
              <a
                className="btn ghost sm"
                href={`/api/applications/${detail.slug}/raw/${current.name}`}
                target="_blank"
                rel="noreferrer"
                title="draft truncated; open the full file"
              >
                view full file
              </a>
            )}
            <button className="btn ghost sm">copy</button>
            <button className="btn ghost sm">open</button>
          </div>
        </div>
        {current?.truncated && (
          <div style={{ padding: '10px 14px', fontSize: 12, color: 'var(--fg-muted)', borderBottom: '1px solid var(--border)', background: 'var(--bg-sunk)' }}>
            <Icon name="flag" size={11} style={{ verticalAlign: 'middle', color: 'var(--accent)' }} />{' '}
            preview truncated to 64&nbsp;KB — full file at <span className="mono-sm">/api/applications/{detail.slug}/raw/{current.name}</span>
          </div>
        )}
        {current?.body === '__PDF_PREVIEW__' ? (
          <PdfPreview name={current.name} />
        ) : current?.body ? (
          <pre className="code" style={{ border: 'none', borderRadius: 0, margin: 0, maxHeight: 560 }}>{current.body}</pre>
        ) : (
          <div style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
            <div className="mono-sm">no content yet</div>
            <div style={{ fontSize: 12, marginTop: 6 }}>this artifact has not been written for {detail.slug}.</div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── PdfPreview ───────────────────────────────────────────────────────────────

interface PdfPreviewProps {
  name: string;
}

function PdfPreview({ name }: PdfPreviewProps) {
  return (
    <div style={{ padding: 24, background: 'var(--bg-sunk)', minHeight: 520 }}>
      <div style={{
        background: '#fff', color: '#111', maxWidth: 540, margin: '0 auto',
        padding: '48px 56px', boxShadow: 'var(--shadow-md)', fontFamily: 'Inter, sans-serif',
        aspectRatio: '8.5 / 11', minHeight: 480, borderRadius: 4,
      }}>
        <div style={{ fontSize: 22, fontWeight: 600, letterSpacing: '-0.02em' }}>jordan smith</div>
        <div style={{ fontSize: 12, color: '#666', marginBottom: 18, fontFamily: 'JetBrains Mono, monospace' }}>jordan@smith.dev · github.com/jsmith · sf, ca</div>
        <div style={{ height: 1, background: '#ddd', margin: '12px 0 16px' }} />
        <div style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.08em', color: '#333', marginBottom: 6 }}>experience</div>
        <div style={{ fontSize: 13, fontWeight: 600 }}>Recurly Engineering · Senior Engineer</div>
        <div style={{ fontSize: 11, color: '#666', marginBottom: 6 }}>2022 — present</div>
        <ul style={{ margin: 0, paddingLeft: 16, fontSize: 11, color: '#222', lineHeight: 1.55 }}>
          <li>Rebuilt deploy pipeline; cut median deploy time 11m → 2m20s.</li>
          <li>Designed artifact-cache layer (Rust + S3) serving 1.2B req/mo at p99 &lt; 38ms.</li>
          <li>Migrated 320 services off legacy scheduler; recovered ~$140k/yr in idle compute.</li>
          <li>Built live-reload dev env used by ~180 engineers; cold-start 18s → 3s.</li>
        </ul>
        <div style={{ marginTop: 14, fontSize: 10, color: '#999', fontFamily: 'JetBrains Mono, monospace' }}>{name} · rendered by jobsmith via quarto</div>
      </div>
    </div>
  );
}

// ── FactCheckTab ─────────────────────────────────────────────────────────────

interface FactCheckTabProps {
  detail: TApplicationDetail;
}

interface FactClaim {
  claim?: string;
  source?: string;
  ok?: boolean;
  [key: string]: unknown;
}

function FactCheckTab({ detail }: FactCheckTabProps) {
  const raw = (detail.fact_check?.claims ?? []) as unknown;
  const claims: FactClaim[] = Array.isArray(raw)
    ? raw.filter((c): c is FactClaim => typeof c === 'object' && c !== null)
    : [];
  const verified = claims.filter(c => c.ok === true).length;

  if (claims.length === 0) {
    return (
      <div className="card">
        <div className="card-h">
          <h3>fact-check</h3>
          <span className="sub">cover_draft.md → master/work.yml</span>
        </div>
        <div style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
          <div className="mono-sm">no fact-check yet</div>
          <div style={{ fontSize: 12, marginTop: 6 }}>fact_check.json has not been written for {detail.slug}.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-h">
        <h3>fact-check</h3>
        <span className="sub">cover_draft.md → master/work.yml</span>
        <div className="right">
          <Badge kind="success">{verified}/{claims.length} verified</Badge>
        </div>
      </div>
      <table className="table">
        <thead>
          <tr><th>claim</th><th>source</th><th>status</th></tr>
        </thead>
        <tbody>
          {claims.map((c, i) => (
            <tr key={i}>
              <td style={{ fontSize: 13 }}>{c.claim ?? '—'}</td>
              <td><span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{c.source ?? '—'}</span></td>
              <td>
                {c.ok
                  ? <Badge kind="success">verified</Badge>
                  : <Badge kind="danger">unverified</Badge>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── AnchorCheckTab ───────────────────────────────────────────────────────────

interface AnchorCheckTabProps {
  detail: TApplicationDetail;
}

function AnchorCheckTab({ detail }: AnchorCheckTabProps) {
  const ac = detail.anchor_check ?? {};
  const preserved: string[] = Array.isArray(ac.preserved_anchors)
    ? (ac.preserved_anchors as unknown[]).filter((s): s is string => typeof s === 'string')
    : [];
  const dropped: Array<{ id?: string; reason?: string }> = Array.isArray(ac.dropped_anchors)
    ? (ac.dropped_anchors as Array<Record<string, unknown>>).map(d => ({
        id: typeof d.id === 'string' ? d.id : undefined,
        reason: typeof d.reason === 'string' ? d.reason : undefined,
      }))
    : [];
  const total = typeof ac.total_anchors === 'number' ? ac.total_anchors : preserved.length + dropped.length;
  const preservedCount = typeof ac.preserved === 'number' ? ac.preserved : preserved.length;

  if (preserved.length === 0 && dropped.length === 0 && !ac.total_anchors) {
    return (
      <div className="card">
        <div className="card-h">
          <h3>anchor preservation</h3>
          <span className="sub">bullet_selection.json</span>
        </div>
        <div style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
          <div className="mono-sm">no anchor check yet</div>
          <div style={{ fontSize: 12, marginTop: 6 }}>anchor_check.json has not been written for {detail.slug}.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-h">
        <h3>anchor preservation</h3>
        <span className="sub">bullet_selection.json</span>
        <div className="right"><Badge kind="success">{preservedCount} / {total} preserved</Badge></div>
      </div>
      <div style={{ padding: '18px 20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
        <div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>preserved anchors</div>
          {preserved.length === 0 && (
            <div className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>none reported</div>
          )}
          {preserved.map(a => (
            <div key={a} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0' }}>
              <Icon name="check" size={11} style={{ color: 'var(--success)' }} />
              <span className="mono-sm">{a}</span>
            </div>
          ))}
        </div>
        <div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>dropped (with reasons)</div>
          {dropped.length === 0 ? (
            <div style={{ padding: '24px', textAlign: 'center', color: 'var(--fg-subtle)', background: 'var(--bg-sunk)', borderRadius: 'var(--radius)', border: '1px dashed var(--border)' }}>
              <div className="mono-sm">none</div>
              <div style={{ fontSize: 12, marginTop: 4 }}>every anchor made it into this draft.</div>
            </div>
          ) : (
            dropped.map((d, i) => (
              <div key={d.id ?? i} style={{ padding: '8px 0', borderBottom: '1px solid var(--border)' }}>
                <div className="mono-sm">{d.id ?? '—'}</div>
                <div style={{ fontSize: 12, color: 'var(--fg-muted)' }}>{d.reason ?? 'no reason given'}</div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

// ── ConfigTab ────────────────────────────────────────────────────────────────

interface ConfigTabProps {
  detail: TApplicationDetail;
}

function ConfigTab({ detail }: ConfigTabProps) {
  const cfg = detail.config ?? {};
  const cfgYaml = JSON.stringify(cfg, null, 2);
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div className="card">
        <div className="card-h"><h3>.apply-config.yaml</h3></div>
        <pre className="code" style={{ border: 'none', borderRadius: 0, margin: 0 }}>{cfgYaml || '{}'}</pre>
      </div>
      <div className="card">
        <div className="card-h"><h3>run options</h3></div>
        <div style={{ padding: '16px 18px' }}>
          <div className="field"><label>job url</label><input className="mono" defaultValue={detail.url} /></div>
          <div className="field"><label>jd-text-file</label><input className="mono" placeholder="(none — fetched from url)" /></div>
          <div className="field">
            <label>verbosity</label>
            <select>
              <option>−v</option>
              <option>−vv</option>
            </select>
          </div>
          <button className="btn primary"><Icon name="play" size={12} /> apply</button>
        </div>
      </div>
    </div>
  );
}

// ── ApplicationDetail (top-level export) ─────────────────────────────────────

export function ApplicationDetail({ slug, back }: ApplicationDetailProps) {
  const query = useApplication(slug);

  // Loading state — match dashboard.tsx pattern (centered mono-sm note).
  if (query.isLoading) {
    return (
      <div className="content wide">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <button className="btn ghost sm" onClick={back}>
            <Icon name="arrow" size={12} style={{ transform: 'scaleX(-1)' }} /> applications
          </button>
        </div>
        <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
          <span className="mono-sm">loading {slug}…</span>
        </div>
      </div>
    );
  }

  // Error state — 404 → "not found", anything else → retry button.
  if (query.isError) {
    const err = query.error as { status?: number } | undefined;
    const isNotFound = err?.status === 404;
    return (
      <div className="content wide">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <button className="btn ghost sm" onClick={back}>
            <Icon name="arrow" size={12} style={{ transform: 'scaleX(-1)' }} /> applications
          </button>
        </div>
        <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--danger, #c43)' }}>
          {isNotFound ? (
            <>
              <div className="mono-sm" style={{ marginBottom: 8 }}>application not found</div>
              <div style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>no slug matched <span className="mono-sm">{slug}</span>.</div>
            </>
          ) : (
            <>
              <div className="mono-sm" style={{ marginBottom: 8 }}>failed to load {slug}</div>
              <button className="btn ghost sm" onClick={() => query.refetch()}>retry</button>
            </>
          )}
        </div>
      </div>
    );
  }

  if (!query.data) {
    // Shouldn't happen given isLoading/isError gates, but render a safe fallback.
    return (
      <div className="content wide">
        <div className="card" style={{ padding: 60, textAlign: 'center', color: 'var(--fg-subtle)' }}>
          <span className="mono-sm">no data</span>
        </div>
      </div>
    );
  }

  return <ApplicationDetailReady detail={query.data} back={back} />;
}

interface ApplicationDetailReadyProps {
  detail: TApplicationDetail;
  back: () => void;
}

/** Best-effort parse of a 409 response body into RerunConflictResponse. */
function parseRerunConflict(err: unknown): RerunConflictResponse | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  try {
    const parsed = JSON.parse(err.body) as Partial<RerunConflictResponse>;
    if (
      typeof parsed.slug === 'string' &&
      typeof parsed.run_id === 'string' &&
      parsed.status === 'running'
    ) {
      return parsed as RerunConflictResponse;
    }
  } catch {
    // not JSON
  }
  return null;
}

/** Best-effort detail string extraction for non-409 ApiErrors. */
function rerunErrorDetail(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.body) as { detail?: unknown };
      if (typeof parsed.detail === 'string') return parsed.detail;
    } catch {
      // body wasn't JSON
    }
    return err.body || err.message;
  }
  if (err instanceof Error) return err.message;
  return 'unknown error';
}

function ApplicationDetailReady({ detail, back }: ApplicationDetailReadyProps) {
  const { progress: initialProgress, activePhase: initialActivePhase } =
    deriveProgress(detail);

  const [tab, setTab] = useState<TabName>('pipeline');
  const [activePhase, setActivePhase] = useState<number>(initialActivePhase);
  const [running, setRunning] = useState<boolean>(detail.status === 'running');
  const [progress, setProgress] = useState<ProgressMap>(initialProgress);

  // Slice 4 (feat-7784ef64): re-run apply mutation. The 409 branch is
  // load-bearing UX — surface the in-flight run_id and let the user watch
  // it themselves; do NOT auto-redirect or auto-cancel.
  const rerunMut = useRerunApplication(detail.slug);
  const rerunConflict = parseRerunConflict(rerunMut.error);
  const rerunErrorMsg =
    rerunMut.isError && !rerunConflict ? rerunErrorDetail(rerunMut.error) : null;

  function handleRerun() {
    rerunMut.reset();
    rerunMut.mutate(
      { verbosity: '-v', force: false },
      {
        onSuccess: () => {
          // Switch to the pipeline tab so the live SSE stream is visible
          // immediately. The Application detail will refetch via the
          // mutation's invalidateQueries, picking up the new run state.
          setTab('pipeline');
          setRunning(true);
        },
      },
    );
  }

  // Live progress sim when running — kept until backend can compute progress
  // server-side and stream it. The actual event log is now driven by SSE
  // inside PipelineTab itself (see feat-440324f1).
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => {
      setProgress(p => {
        const next: ProgressMap = { ...p };
        const cur = (p[3] < 100 ? 3 : p[2] < 100 ? 2 : 1) as 1 | 2 | 3;
        if (cur === 1 && p[1] >= 100) return p;
        next[cur] = Math.min(100, p[cur] + Math.random() * 7 + 2);
        return next;
      });
    }, 700);
    return () => clearInterval(id);
  }, [running]);

  const allDone = progress[1] >= 100 && progress[2] >= 100 && progress[3] >= 100;
  useEffect(() => { if (allDone) setRunning(false); }, [allDone]);

  return (
    <div className="content wide">
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <button className="btn ghost sm" onClick={back}>
          <Icon name="arrow" size={12} style={{ transform: 'scaleX(-1)' }} /> applications
        </button>
      </div>
      <div className="page-head">
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <h1 style={{ margin: 0 }}>{detail.role ?? '—'}</h1>
            <StatusBadge status={running ? 'running' : detail.status} />
          </div>
          <div style={{ display: 'flex', gap: 14, marginTop: 8, color: 'var(--fg-muted)', fontSize: 13 }}>
            <span>{detail.company ?? '—'}</span>
            <span style={{ color: 'var(--fg-subtle)' }}>·</span>
            <span className="mono-sm">{detail.slug}</span>
            <span style={{ color: 'var(--fg-subtle)' }}>·</span>
            <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>updated {detail.updated}</span>
          </div>
        </div>
        <div className="actions">
          <button className="btn"><Icon name="doc" size={13} /> open in marimo</button>
          <button className="btn"><Icon name="folder" size={13} /> reveal in finder</button>
          {!running && (
            <button
              className="btn primary"
              onClick={handleRerun}
              disabled={rerunMut.isPending}
            >
              {rerunMut.isPending
                ? <><span className="spin" /> starting…</>
                : <><Icon name="play" size={12} /> re-run apply</>}
            </button>
          )}
          {running && (
            <button className="btn danger" onClick={() => setRunning(false)}>
              <Icon name="x" size={12} /> cancel run
            </button>
          )}
        </div>
      </div>

      {rerunConflict && (
        <div
          role="alert"
          style={{
            margin: '6px 0 14px',
            padding: '10px 14px',
            background: 'var(--bg-sunk)',
            border: '1px solid var(--accent, #d49a3a)',
            borderRadius: 'var(--radius)',
            fontSize: 12.5,
            color: 'var(--fg)',
          }}
        >
          <Icon name="flag" size={11} style={{ verticalAlign: 'middle', color: 'var(--accent, #d49a3a)' }} />{' '}
          a run is already in progress for this application
          {' '}(<span className="mono-sm">run_id: {rerunConflict.run_id}</span>).
          {' '}Watch it in the <button
            className="btn ghost sm"
            style={{ padding: '0 6px', marginLeft: 4 }}
            onClick={() => setTab('pipeline')}
          >Pipeline tab</button>.
        </div>
      )}

      {rerunErrorMsg && (
        <div
          role="alert"
          style={{
            margin: '6px 0 14px',
            padding: '10px 14px',
            background: 'var(--bg-sunk)',
            border: '1px solid var(--danger, #c43)',
            borderRadius: 'var(--radius)',
            fontSize: 12.5,
            color: 'var(--danger, #c43)',
          }}
        >
          <div className="mono-sm" style={{ marginBottom: 2 }}>could not start run</div>
          <div style={{ color: 'var(--fg-muted)' }}>{rerunErrorMsg}</div>
        </div>
      )}

      <div className="pipeline" style={{ marginBottom: 20 }}>
        {PHASES.map((p, i) => {
          const pr = progress[p.num];
          const firstIncomplete = ([1, 2, 3] as const).findIndex(n => progress[n] < 100);
          const status: PhaseStatus = pr >= 100
            ? 'done'
            : (running && i === firstIncomplete ? 'running' : 'queued');
          return (
            <PhaseCard
              key={p.num}
              num={p.num}
              name={p.name}
              blurb={p.blurb}
              status={status}
              progress={pr}
              active={activePhase === p.num}
              onClick={() => setActivePhase(p.num)}
              meta={[
                { v: p.specs.length, k: 'specialists' },
                {
                  v: pr >= 100 ? phaseDuration(p.num) : (status === 'running' ? 'live' : '—'),
                  k: status === 'running' ? '' : 'duration',
                },
              ]}
            />
          );
        })}
      </div>

      <div className="tabs">
        {(['pipeline', 'artifacts', 'factcheck', 'anchors', 'config'] as TabName[]).map(t => (
          <div key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>{t}</div>
        ))}
      </div>

      {tab === 'pipeline' && <PipelineTab slug={detail.slug} running={running} phase={activePhase} progress={progress} />}
      {tab === 'artifacts' && <ArtifactsTab detail={detail} />}
      {tab === 'factcheck' && <FactCheckTab detail={detail} />}
      {tab === 'anchors' && <AnchorCheckTab detail={detail} />}
      {tab === 'config' && <ConfigTab detail={detail} />}
    </div>
  );
}
