// application.tsx — port of design/app/application.jsx
//
// Exports: ApplicationDetail (top-level).
// Sub-components (PhaseCard, PipelineTab, ArtifactsTab, FactCheckTab,
// AnchorCheckTab, ConfigTab) are file-local.
//
// DOM structure, class names, and visual behaviour are pixel-identical to
// the design source.

import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import type { SampleApp, AppPhase, AppStatus, IconName } from '../types';
import { Icon, Badge, StatusBadge } from './shared';
import { useApplication } from '../api/hooks';
import { JobsmithApiError, postApplication, buildEventsUrl, redactSensitive, apiGet, apiPost, apiPut, apiGetBlob, apiGetText, notifyDataChanged } from '../api/client';
import type {
  ApplicationDetail as ApiApplicationDetail,
  ApplicationArtifact as ApiApplicationArtifact,
} from '../api/types';
import { useProposal } from './proposalContext';
import { lineDiff } from '../utils/diff';

// ── Public prop type ─────────────────────────────────────────────────────────

export interface ApplicationDetailProps {
  /** The application slug to display. Falls back to SAMPLE_APPS[0] if not found. */
  slug: string;
  /** Navigate back to the applications list. */
  back: () => void;
}

// ── Phase-related helpers ────────────────────────────────────────────────────

type PhaseStatus = 'done' | 'running' | 'queued' | 'failed';
type SpecialistRunStatus = 'queued' | 'running' | 'done' | 'failed';
type SpecialistStatusMap = Record<string, SpecialistRunStatus>;

interface PhaseSpec {
  num: 1 | 2 | 3;
  name: string;
  blurb: string;
  specs: string[];
}

// Specialist names mirror the canonical PHASE_SPECIALISTS map in
// src/jobsmith/_state_readers.py. Keep these two in sync — divergence
// surfaces as "specialist X failed" panel rows for specialists the
// orchestrator never actually dispatched.
const PHASES: PhaseSpec[] = [
  {
    num: 1,
    name: 'gather',
    blurb: 'parse JD, score fit, dispatch gather specialists',
    specs: [
      'apply-jd-parser',
      'apply-fit-scorer',
      'apply-hm-enricher',
      'apply-bullet-selector',
      'apply-company-research',
    ],
  },
  {
    num: 2,
    name: 'draft',
    blurb: 'prose-writer + prose-qa loop until pass',
    specs: ['apply-prose-writer', 'apply-prose-qa'],
  },
  {
    num: 3,
    name: 'render',
    blurb: 'render resume + cover letter + index, ATS check',
    specs: [
      'apply-resume-renderer',
      'apply-portfolio-ats-checker',
      'apply-visual-layout-reviewer',
      'apply-cover-letter-writer',
      'apply-index-writer',
    ],
  },
];

// ── Event stream helpers ─────────────────────────────────────────────────────

/**
 * One row in the event-stream log.
 *
 * - Plain stdout/stderr from the apply CLI lands as `kind: 'log'` (or undefined,
 *   for back-compat). The `lvl` field carries 'info' / 'warn' for those.
 * - Structured agent activity tailed from transcript.jsonl lands with
 *   `kind: 'tool_call' | 'tool_result' | 'agent_text' | 'phase_boundary'`
 *   (bug-0e13706c). The renderer switches on kind to format these as
 *   tool/result rows rather than raw terminal text.
 */
interface LogEvent {
  ts: string;
  lvl: string;
  msg: string;
  kind?: 'log' | 'tool_call' | 'tool_result' | 'agent_text' | 'phase_boundary';
  toolName?: string;
  toolInputPreview?: string;
  toolUseId?: string;
  status?: string;
  phaseName?: string;
}

/**
 * SSE payload shape for `event=transcript` — bug-0e13706c.
 * The supervisor tails transcript.jsonl and forwards each new JSON line,
 * preserving the renderer's record format. We only consume a few fields;
 * unknown event types are still rendered (as kind=log) so future renderer
 * additions show up automatically.
 */
interface SseTranscriptEvent {
  run_id: string;
  payload: {
    ts?: string;
    type?: string;
    tool_name?: string;
    tool_input_truncated?: string;
    tool_use_id?: string;
    text_truncated?: string;
    result_truncated?: string;
    _phase_boundary?: string;
    [k: string]: unknown;
  };
}

/** Convert a transcript SSE payload into a LogEvent for the event stream. */
function transcriptToLogEvent(t: SseTranscriptEvent): LogEvent | null {
  const p = t.payload;
  // Phase-boundary marker (rendered as a header row).
  if (typeof p._phase_boundary === 'string') {
    return {
      ts: now(),
      lvl: 'phase',
      msg: `── phase: ${p._phase_boundary} ──`,
      kind: 'phase_boundary',
      phaseName: p._phase_boundary,
    };
  }
  if (p.type === 'tool_call') {
    const tname = String(p.tool_name ?? '?');
    const preview = String(p.tool_input_truncated ?? '').slice(0, 80);
    return {
      ts: now(),
      lvl: 'tool',
      msg: preview ? `${tname}(${preview})` : `${tname}()`,
      kind: 'tool_call',
      toolName: tname,
      toolInputPreview: preview,
      toolUseId: typeof p.tool_use_id === 'string' ? p.tool_use_id : undefined,
    };
  }
  if (p.type === 'tool_result') {
    const summary = String(p.result_truncated ?? '').slice(0, 80);
    return {
      ts: now(),
      lvl: 'result',
      msg: summary || '✓',
      kind: 'tool_result',
      toolUseId: typeof p.tool_use_id === 'string' ? p.tool_use_id : undefined,
    };
  }
  if (p.type === 'text') {
    const txt = String(p.text_truncated ?? '').slice(0, 200);
    if (!txt) return null;
    return {
      ts: now(),
      lvl: 'agent',
      msg: txt,
      kind: 'agent_text',
    };
  }
  // Unknown payload type — drop silently rather than render terminal-formatted.
  return null;
}

function now(): string {
  return new Date().toTimeString().slice(0, 8);
}

function phaseDuration(n: number): string {
  return (['1.4s', '3.8s', '12.1s'] as const)[n - 1] ?? '—';
}

// The event log starts empty — entries are appended ONLY from the real SSE
// stream subscribed in subscribeToEvents(). Previously this seeded a 15-line
// fabricated event sequence (see GH#52); rendering those alongside live SSE
// data made the UI lie about what the pipeline was doing.

// ── Tab type ─────────────────────────────────────────────────────────────────

type TabName = 'pipeline' | 'review' | 'documents' | 'artifacts' | 'factcheck' | 'anchors' | 'config';
const TAB_ORDER: TabName[] = ['pipeline', 'review', 'documents', 'artifacts', 'factcheck', 'anchors', 'config'];

// ── Progress map type ────────────────────────────────────────────────────────

type ProgressMap = Record<1 | 2 | 3, number>;

// ── Progress derivation helper ───────────────────────────────────────────────

function deriveProgress(app: SampleApp): {
  progress: ProgressMap;
  activePhase: 1 | 2 | 3;
  failedPhaseNum: 1 | 2 | 3 | null;
} {
  // If rendered or done, all phases complete
  if (app.status === 'rendered' || app.status === 'done') {
    return {
      progress: { 1: 100, 2: 100, 3: 100 },
      activePhase: 3,
      failedPhaseNum: null,
    };
  }

  // Incomplete: phases 1+2 done, render not yet run
  if ((app.status as string) === 'incomplete') {
    return {
      progress: { 1: 100, 2: 100, 3: 0 },
      activePhase: 3,
      failedPhaseNum: null,
    };
  }

  // For failed status, only mark phases STRICTLY BEFORE the failed phase
  // as 100. The failed phase itself stays at 0 so PipelineTab can paint
  // it 'failed' rather than 'done' (closes roborev job 965 MEDIUM).
  // ``failedPhaseNum`` is seeded from the numeric phase that ``fromApi``
  // already mapped from the API's phase label, so a reload of a failed
  // run shows which phase failed without waiting for a live SSE event
  // (closes roborev job 966 MEDIUM). ``fromApi`` falls back to phase 1
  // for ``unknown``/missing labels — we cannot prove a real failed
  // phase from that, so ``failedPhaseNum`` stays null in those cases
  // and the active card defaults to phase 1.
  if (app.status === 'failed') {
    const apiPhase = (api: SampleApp): 1 | 2 | 3 | null => {
      const p = api.phase;
      if (p === 1 || p === 2 || p === 3) return p;
      return null;
    };
    const knownPhase: 1 | 2 | 3 | null = apiPhase(app);
    const failedPhase: 1 | 2 | 3 = knownPhase ?? 1;
    const progress: ProgressMap = { 1: 0, 2: 0, 3: 0 };
    // Earlier phases got there (passed); the failed phase did NOT.
    for (const n of [1, 2, 3] as const) {
      if (n < failedPhase) progress[n] = 100;
    }
    return {
      progress,
      activePhase: failedPhase,
      failedPhaseNum: knownPhase,
    };
  }

  // For running statuses: initialize based on phase
  // Phase 0 (queued) starts at nothing
  if (app.phase === 0) {
    return {
      progress: { 1: 0, 2: 0, 3: 0 },
      activePhase: 1,
      failedPhaseNum: null,
    };
  }

  // Phase 1 (gather) running or gather status
  if (app.phase === 1 || app.status === 'gather') {
    return {
      progress: { 1: app.status === 'running' ? 42 : 0, 2: 0, 3: 0 },
      activePhase: 1,
      failedPhaseNum: null,
    };
  }

  // Phase 2 (draft) running or draft status
  if (app.phase === 2 || app.status === 'draft') {
    return {
      progress: { 1: 100, 2: app.status === 'running' ? 42 : 0, 3: 0 },
      activePhase: 2,
      failedPhaseNum: null,
    };
  }

  // Phase 3 (render) running or review status
  if (app.phase === 3 || app.status === 'review') {
    return {
      progress: { 1: 100, 2: 100, 3: app.status === 'running' ? 42 : 0 },
      activePhase: 3,
      failedPhaseNum: null,
    };
  }

  // Default fallback (should not reach here)
  return {
    progress: { 1: 0, 2: 0, 3: 0 },
    activePhase: 1,
    failedPhaseNum: null,
  };
}

function mergeArtifactProgress(
  api: ApiApplicationDetail | undefined,
  base: ProgressMap,
): ProgressMap {
  const doneSpecialists = new Set(
    (api?.artifacts ?? [])
      .map(a => a.specialist)
      .filter((s): s is string => Boolean(s)),
  );
  if (doneSpecialists.size === 0) return base;

  const progress: ProgressMap = { ...base };
  for (const phase of PHASES) {
    const doneCount = phase.specs.filter(s => doneSpecialists.has(s)).length;
    if (doneCount === 0) continue;
    progress[phase.num] = doneCount >= phase.specs.length
      ? 100
      : Math.max(progress[phase.num], Math.min(90, 10 + doneCount * 15));
  }
  return progress;
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
          {status === 'failed' && <><Icon name="x" size={12} style={{ color: 'var(--danger, #e55)' }} /> failed</>}
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

// ── EventLogRow ──────────────────────────────────────────────────────────────
//
// One row in the event-stream log. Branches on `kind` (bug-0e13706c) so
// structured events from transcript.jsonl render as typed rows (tool / result
// / agent text / phase boundary) instead of pre-formatted terminal log lines.

function EventLogRow({ e }: { e: LogEvent }) {
  if (e.kind === 'phase_boundary') {
    return (
      <div style={{
        padding: '6px 0',
        margin: '4px 0',
        borderTop: '1px solid var(--border)',
        borderBottom: '1px solid var(--border)',
        background: 'var(--bg-sunk)',
        fontWeight: 500,
        fontSize: 12,
        textAlign: 'center',
        letterSpacing: '0.04em',
        textTransform: 'uppercase',
        color: 'var(--fg-muted)',
      }}>
        phase: {e.phaseName ?? '?'}
      </div>
    );
  }
  if (e.kind === 'tool_call') {
    return (
      <div>
        <span className="ts">{e.ts}</span>
        <span className="lvl tool" style={{ color: 'var(--accent)' }}>{'tool  '}</span>
        <span className="msg">
          <strong style={{ color: 'var(--fg)' }}>{e.toolName ?? '?'}</strong>
          {e.toolInputPreview ? (
            <span style={{ color: 'var(--fg-subtle)' }}>{' '}({e.toolInputPreview})</span>
          ) : null}
        </span>
      </div>
    );
  }
  if (e.kind === 'tool_result') {
    return (
      <div>
        <span className="ts">{e.ts}</span>
        <span className="lvl result" style={{ color: 'var(--success, #5a5)' }}>{'✓     '}</span>
        <span className="msg" style={{ color: 'var(--fg-subtle)' }}>{e.msg}</span>
      </div>
    );
  }
  if (e.kind === 'agent_text') {
    return (
      <div style={{ padding: '4px 8px', borderLeft: '2px solid var(--border)', margin: '2px 0' }}>
        <span className="ts">{e.ts}</span>
        <span className="lvl agent" style={{ color: 'var(--fg-muted)' }}>{'agent '}</span>
        <span className="msg" style={{ fontStyle: 'italic' }}>{e.msg}</span>
      </div>
    );
  }
  // Default: legacy log line. Existing styles + dangerouslySetInnerHTML for the
  // pre-redacted/escaped HTML in `msg`.
  return (
    <div>
      <span className="ts">{e.ts}</span>
      <span className={`lvl ${e.lvl}`}>{e.lvl.padEnd(6)}</span>
      <span className="msg" dangerouslySetInnerHTML={{ __html: e.msg }} />
    </div>
  );
}

// ── PipelineTab ──────────────────────────────────────────────────────────────

interface PipelineTabProps {
  events: LogEvent[];
  running: boolean;
  phase: number;
  progress: ProgressMap;
  /**
   * SSE-derived terminal status. When 'failed', the *failedPhaseNum*'s
   * specialists render as 'failed' rather than perpetually 'running'
   * (bug-8ade6f70). 'done' / 'rendered' map to 100% completion.
   */
  sseStatus: AppStatus | null;
  /**
   * Phase number that received the failure (1=gather, 2=draft, 3=render).
   * Only that phase's specialists render as 'failed'; queued specialists
   * for downstream phases remain 'queued'. Null when no failure pinned
   * yet (closes roborev job 953 LOW).
   */
  failedPhaseNum: 1 | 2 | 3 | null;
  specialistStatuses: SpecialistStatusMap;
}

function PipelineTab({ events, running, phase, progress, sseStatus, failedPhaseNum, specialistStatuses }: PipelineTabProps) {
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [events]);

  const phaseSpec = PHASES[phase - 1];
  const phaseNum = phaseSpec.num;
  const phaseDone = progress[phaseNum] >= 100;

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 360px', gap: 16 }}>
      <div className="card">
        <div className="card-h">
          <h3>event stream</h3>
          <span className="sub">phase {phase} · {events.length} events</span>
          <div className="right">
            <button className="btn ghost sm">−v</button>
            <button className="btn ghost sm" style={{ borderColor: 'var(--border)', background: 'var(--bg-sunk)' }}>−vv</button>
            <button className="btn ghost sm">copy</button>
          </div>
        </div>
        <div className="eventlog" ref={logRef} style={{ maxHeight: 460, borderRadius: 0, border: 'none' }}>
          {events.map((e, i) => <EventLogRow key={i} e={e} />)}
          {running && (
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
              const phaseFailed = sseStatus === 'failed' && !phaseDone && failedPhaseNum === phaseNum;
              const status = specialistStatuses[s] ?? (phaseDone ? 'done' : 'queued');
              const effectiveStatus = status === 'queued' && phaseFailed ? 'queued' : status;
              const iconName: IconName = effectiveStatus === 'done' ? 'check' : (effectiveStatus === 'failed' ? 'x' : 'dot');
              const iconColor = effectiveStatus === 'done'
                ? 'var(--success)'
                : (effectiveStatus === 'failed' ? 'var(--danger, #e55)' : 'var(--accent)');
              const label = effectiveStatus === 'done' && phaseDone
                ? 'complete'
                : effectiveStatus;
              return (
                <div key={s} style={{ padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 10 }}>
                  <Icon name={iconName} size={12} style={{ color: iconColor }} />
                  <span className="mono-sm" style={{ flex: 1 }}>{s}</span>
                  <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>{label}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/*
          The "db writes" panel previously displayed hardcoded row counts
          (apply_runs 1, spec 1, bullet_selection 14, …) regardless of the
          actual pipeline state (GH#52). Removed entirely until an API
          endpoint exposes real counts; reintroduce here when it does.
        */}
      </div>
    </div>
  );
}

// ── ArtifactsTab ─────────────────────────────────────────────────────────────

interface ArtifactsTabProps {
  artifacts: ApiApplicationArtifact[];
}

// API artifact `kind` values to friendly display names.
const ARTIFACT_KIND_LABELS: Record<string, string> = {
  'jd-parsed': 'spec.json',
  'bullet-selection': 'bullet_selection.json',
  'cover-draft': 'cover_draft.md',
  'fact-check': 'fact_check.json',
  'anchor-check': 'anchor_check.json',
};

function artifactDisplayName(kind: string): string {
  return ARTIFACT_KIND_LABELS[kind] ?? `${kind}.json`;
}

function ArtifactsTab({ artifacts }: ArtifactsTabProps) {
  const [sel, setSel] = useState<string | null>(
    artifacts.length > 0 ? artifacts[0].kind : null,
  );

  if (artifacts.length === 0) {
    return (
      <div className="card" style={{ padding: 32, textAlign: 'center', color: 'var(--fg-muted)' }}>
        <div className="mono-sm" style={{ marginBottom: 6 }}>no artifacts yet</div>
        <div style={{ fontSize: 13 }}>
          this run hasn't produced any specialist outputs. once the pipeline
          writes to <code>.apply-state/</code> the artifacts will appear here.
        </div>
      </div>
    );
  }

  const selectedArtifact = artifacts.find(a => a.kind === sel) ?? artifacts[0];
  const previewJson = JSON.stringify(selectedArtifact.output, null, 2);

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
      <div className="card" style={{ padding: '12px' }}>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', padding: '4px 8px 8px' }}>
          artifacts ({artifacts.length})
        </div>
        <div className="tree">
          <div className="tree-children">
            {artifacts.map(a => (
              <div
                key={`${a.run_id}-${a.kind}-${a.version ?? 0}`}
                className={`tree-row ${sel === a.kind ? 'active' : ''}`}
                onClick={() => setSel(a.kind)}
              >
                <span className="caret" />
                <Icon name="doc" size={11} className="ico" />
                <span style={{ flex: 1 }}>{artifactDisplayName(a.kind)}</span>
                <span style={{ color: 'var(--fg-subtle)', fontSize: 10.5 }}>
                  {a.specialist}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-h">
          <Icon name="doc" size={13} />
          <h3 style={{ fontFamily: 'var(--font-mono)', fontWeight: 500 }}>
            {artifactDisplayName(selectedArtifact.kind)}
          </h3>
          <span className="sub" style={{ color: 'var(--fg-subtle)' }}>
            {selectedArtifact.specialist}
            {selectedArtifact.finished_at ? ` · ${selectedArtifact.finished_at}` : ''}
          </span>
        </div>
        <pre className="code" style={{ border: 'none', borderRadius: 0, margin: 0, maxHeight: 560 }}>
          {previewJson}
        </pre>
      </div>
    </div>
  );
}

// ── DocumentsTab ─────────────────────────────────────────────────────────────

function DocumentsTab({ slug }: { slug: string }) {
  const [pdfBlobUrl, setPdfBlobUrl] = useState<string | null>(null);
  const [coverLetter, setCoverLetter] = useState<string | null>(null);
  const [coverLetterName, setCoverLetterName] = useState<string>('cover-letter.md');
  const [loading, setLoading] = useState(true);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    let blobUrl: string | null = null;
    let cancelled = false;

    async function load() {
      try {
        const names: string[] = await apiGet<string[]>(`/api/applications/${slug}/documents`).catch((): string[] => []);
        const resumeName = names.includes('resume.pdf') ? 'resume.pdf' : null;
        const coverName = names.includes('cover-letter.md')
          ? 'cover-letter.md'
          : (names.includes('cover-letter-draft.md') ? 'cover-letter-draft.md' : null);
        setCoverLetterName(coverName ?? 'cover-letter.md');
        const [pdfBlob, clText] = await Promise.allSettled([
          resumeName
            ? apiGetBlob(`/api/applications/${slug}/documents/${resumeName}`)
            : Promise.reject(new Error('resume.pdf not found')),
          coverName
            ? apiGetText(`/api/applications/${slug}/documents/${coverName}`)
            : Promise.reject(new Error('cover letter not found')),
        ]);
        if (cancelled) return;
        if (pdfBlob.status === 'fulfilled') {
          blobUrl = URL.createObjectURL(pdfBlob.value);
          setPdfBlobUrl(blobUrl);
        }
        if (clText.status === 'fulfilled') {
          setCoverLetter(clText.value);
        }
        if (pdfBlob.status === 'rejected' && clText.status === 'rejected') {
          setMissing(true);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();

    return () => {
      cancelled = true;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  if (loading) {
    return (
      <div className="card" style={{ padding: 32, textAlign: 'center', color: 'var(--fg-muted)' }}>
        loading documents…
      </div>
    );
  }

  if (missing) {
    return (
      <div className="card" style={{ padding: 32, textAlign: 'center', color: 'var(--fg-muted)' }}>
        <div className="mono-sm" style={{ marginBottom: 6 }}>no documents yet</div>
        <div style={{ fontSize: 13 }}>
          the render phase must complete to produce <code>resume.pdf</code> and the cover letter.
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {pdfBlobUrl && (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)', fontWeight: 600, fontSize: 13 }}>
            resume.pdf
          </div>
          <div style={{ background: '#fff' }}>
            <iframe
              src={pdfBlobUrl}
              title="resume.pdf"
              style={{ width: '100%', height: 700, border: 'none', display: 'block', colorScheme: 'light' }}
            />
          </div>
        </div>
      )}
      {coverLetter && (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)', fontWeight: 600, fontSize: 13 }}>
            {coverLetterName}
          </div>
          <pre style={{ margin: 0, padding: '16px', whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.7, fontFamily: 'var(--font-sans, inherit)' }}>
            {coverLetter}
          </pre>
        </div>
      )}
    </div>
  );
}

// ── ReviewTab ────────────────────────────────────────────────────────────────

interface ReviewState {
  cover_letter: string | null;
  fit_score: number | null;
  fit_rationale: string | null;
  fit_concerns: string[];
  review_status: 'pending' | 'approved' | 'needs-revision';
  reviewed_at: string | null;
  canonical_slug: string;
}

const REVIEW_STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pending:          { label: 'pending review', color: 'var(--fg-muted)' },
  approved:         { label: 'approved',        color: 'var(--success, #27ae60)' },
  'needs-revision': { label: 'needs revision',  color: 'var(--warn, #e67e22)' },
};

function ReviewTab({ slug, reviewKey }: { slug: string; reviewKey: number }) {
  const [state, setState] = useState<ReviewState | null>(null);
  const [loading, setLoading] = useState(true);
  const [pdfBlobUrl, setPdfBlobUrl] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [statusSaving, setStatusSaving] = useState(false);

  // Proposal context — for the full-width diff panel.
  const { pendingProposal, applyProposal, rejectProposal } = useProposal();
  // Only show the proposal panel when it belongs to this application slug.
  const activeProposal =
    pendingProposal?.proposal.slug === slug ? pendingProposal : null;

  useEffect(() => {
    let cancelled = false;
    let blobUrl: string | null = null;
    async function load() {
      setLoading(true);
      try {
        const [reviewData, pdfBlob] = await Promise.allSettled([
          apiGet<ReviewState>(`/api/applications/${slug}/review`),
          apiGetBlob(`/api/applications/${slug}/documents/resume.pdf`),
        ]);
        if (cancelled) return;
        if (reviewData.status === 'fulfilled') {
          setState(reviewData.value);
        }
        if (pdfBlob.status === 'fulfilled') {
          blobUrl = URL.createObjectURL(pdfBlob.value);
          setPdfBlobUrl(blobUrl);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => {
      cancelled = true;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, reviewKey]);

  const handleEdit = () => {
    setDraft(state?.cover_letter ?? '');
    setSaveError(null);
    setEditing(true);
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const result = await apiPut<{ saved: boolean; words: number }>(
        `/api/applications/${slug}/cover-letter`,
        { text: draft },
      );
      if (result.saved) {
        setState(s => s ? { ...s, cover_letter: draft } : s);
        setEditing(false);
      }
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const handleSetStatus = async (newStatus: ReviewState['review_status']) => {
    setStatusSaving(true);
    try {
      await apiPost(`/api/applications/${slug}/review-status`, { status: newStatus });
      setState(s => s ? { ...s, review_status: newStatus, reviewed_at: new Date().toISOString() } : s);
    } finally {
      setStatusSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="card" style={{ padding: 32, textAlign: 'center', color: 'var(--fg-muted)' }}>
        loading review…
      </div>
    );
  }

  const wordCount = (state?.cover_letter ?? '').trim().split(/\s+/).filter(Boolean).length;
  const statusInfo = REVIEW_STATUS_LABELS[state?.review_status ?? 'pending'];
  const fitPct = state?.fit_score != null ? Math.round(state.fit_score * 100) : null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* Full-width proposal diff panel */}
      {activeProposal && (
        <div
          className="card"
          role="region"
          aria-label="Proposed cover letter revision"
          style={{ padding: 0, overflow: 'hidden', border: '1px solid var(--accent)' }}
        >
          {/* Panel header */}
          <div style={{
            padding: '12px 16px',
            borderBottom: '1px solid var(--border)',
            background: 'var(--bg-sunk)',
            display: 'flex',
            alignItems: 'flex-start',
            gap: 12,
            flexWrap: 'wrap',
          }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 700, fontSize: 13, marginBottom: activeProposal.proposal.rationale ? 4 : 0 }}>
                ✍️ Proposed revision
                {activeProposal.proposal.summary && (
                  <span style={{ fontWeight: 400, marginLeft: 8, color: 'var(--fg-muted)' }}>
                    — {activeProposal.proposal.summary}
                  </span>
                )}
              </div>
              {activeProposal.proposal.rationale && (
                <div style={{ fontSize: 12, color: 'var(--fg-muted)', lineHeight: 1.5 }}>
                  {activeProposal.proposal.rationale}
                </div>
              )}
            </div>
            <div style={{ display: 'flex', gap: 8, flexShrink: 0, alignItems: 'center' }}>
              <button
                className="btn primary sm"
                onClick={() => { void applyProposal(); }}
                disabled={activeProposal.applying}
                aria-label="Apply proposed cover letter change"
              >
                {activeProposal.applying ? 'Applying…' : 'Apply'}
              </button>
              <button
                className="btn sm ghost"
                onClick={rejectProposal}
                disabled={activeProposal.applying}
                aria-label="Reject proposed cover letter change"
              >
                Reject
              </button>
            </div>
          </div>

          {/* Fact-check failure warning */}
          {activeProposal.failedClaims && (
            <div
              role="alert"
              style={{
                padding: '10px 16px',
                background: 'var(--danger-soft)',
                borderBottom: '1px solid var(--danger)',
                fontSize: 12.5,
                color: 'var(--danger)',
                display: 'flex',
                flexDirection: 'column',
                gap: 4,
              }}
            >
              <span style={{ fontWeight: 600 }}>
                ⚠ Fact-check rejected this revision — the following claims could not be verified:
              </span>
              <ul style={{ margin: '2px 0 0', paddingLeft: 18 }}>
                {activeProposal.failedClaims.map((c, k) => (
                  <li key={k}>{c}</li>
                ))}
              </ul>
              <span style={{ color: 'var(--fg-muted)' }}>Ask the chat to revise it, then apply again.</span>
            </div>
          )}

          {/* Side-by-side diff */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: 0,
            maxHeight: 520,
            overflow: 'hidden',
          }}>
            {/* Old (removed lines) */}
            <div style={{ borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              <div style={{
                padding: '6px 12px',
                fontSize: 11,
                fontWeight: 600,
                color: 'var(--fg-muted)',
                background: 'var(--bg-sunk)',
                borderBottom: '1px solid var(--border)',
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
              }}>
                Before
              </div>
              <pre style={{
                margin: 0,
                padding: '10px 0',
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                lineHeight: 1.55,
                overflowY: 'auto',
                flex: 1,
              }}>
                {lineDiff(
                  activeProposal.oldContent || (state?.cover_letter ?? ''),
                  activeProposal.proposal.new_content,
                ).filter(ln => ln.type !== 'add').map((ln, k) => (
                  <span
                    key={k}
                    style={{
                      display: 'block',
                      padding: '0 12px',
                      background: ln.type === 'del' ? 'var(--danger-soft)' : 'transparent',
                      color: ln.type === 'del' ? 'var(--danger)' : 'var(--fg-muted)',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                    }}
                  >
                    {ln.type === 'del' ? '− ' : '  '}{ln.text || ' '}
                  </span>
                ))}
              </pre>
            </div>
            {/* New (added lines) */}
            <div style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              <div style={{
                padding: '6px 12px',
                fontSize: 11,
                fontWeight: 600,
                color: 'var(--fg-muted)',
                background: 'var(--bg-sunk)',
                borderBottom: '1px solid var(--border)',
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
              }}>
                After
              </div>
              <pre style={{
                margin: 0,
                padding: '10px 0',
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                lineHeight: 1.55,
                overflowY: 'auto',
                flex: 1,
              }}>
                {lineDiff(
                  activeProposal.oldContent || (state?.cover_letter ?? ''),
                  activeProposal.proposal.new_content,
                ).filter(ln => ln.type !== 'del').map((ln, k) => (
                  <span
                    key={k}
                    style={{
                      display: 'block',
                      padding: '0 12px',
                      background: ln.type === 'add' ? 'var(--success-soft)' : 'transparent',
                      color: ln.type === 'add' ? 'var(--success)' : 'var(--fg-muted)',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                    }}
                  >
                    {ln.type === 'add' ? '+ ' : '  '}{ln.text || ' '}
                  </span>
                ))}
              </pre>
            </div>
          </div>
        </div>
      )}

      {/* Header bar */}
      <div className="card" style={{ padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        {fitPct != null && (
          <span style={{ fontWeight: 600, fontSize: 13 }}>
            fit score: <span style={{ color: fitPct >= 75 ? 'var(--success, #27ae60)' : fitPct >= 55 ? 'var(--warn, #e67e22)' : 'var(--danger, #c0392b)' }}>{fitPct}%</span>
          </span>
        )}
        <span style={{ fontSize: 13, color: statusInfo.color, fontWeight: 500 }}>
          ● {statusInfo.label}
        </span>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
          <button
            className="btn sm"
            style={{ opacity: state?.review_status === 'approved' ? 0.5 : 1 }}
            disabled={statusSaving || state?.review_status === 'approved'}
            onClick={() => { void handleSetStatus('approved'); }}
          >
            ✓ approve
          </button>
          <button
            className="btn sm ghost"
            style={{ opacity: state?.review_status === 'needs-revision' ? 0.5 : 1 }}
            disabled={statusSaving || state?.review_status === 'needs-revision'}
            onClick={() => { void handleSetStatus('needs-revision'); }}
          >
            ↩ needs revision
          </button>
        </div>
      </div>

      {/* Split pane */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 420px', gap: 12, alignItems: 'start' }}>
        {/* Left: resume PDF */}
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)', fontWeight: 600, fontSize: 13 }}>
            resume.pdf
          </div>
          {pdfBlobUrl ? (
            <div style={{ background: '#fff' }}>
              <iframe
                src={pdfBlobUrl}
                title="resume.pdf"
                style={{ width: '100%', height: 780, border: 'none', display: 'block', colorScheme: 'light' }}
              />
            </div>
          ) : (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--fg-muted)', fontSize: 13 }}>
              resume not yet rendered — run phase 3
            </div>
          )}
        </div>

        {/* Right: cover letter + fit score */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* Cover letter card */}
          <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
            <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontWeight: 600, fontSize: 13, flex: 1 }}>cover letter</span>
              <span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{wordCount}w</span>
              {!editing && (
                <button className="btn sm ghost" onClick={handleEdit}>edit</button>
              )}
            </div>
            {editing ? (
              <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                <textarea
                  value={draft}
                  onChange={e => setDraft(e.target.value)}
                  style={{
                    width: '100%', minHeight: 360, padding: 10,
                    fontSize: 13, lineHeight: 1.7, fontFamily: 'var(--font-sans, inherit)',
                    border: '1px solid var(--border)', borderRadius: 4,
                    background: 'var(--surface, #fff)', color: 'var(--fg)',
                    resize: 'vertical', boxSizing: 'border-box',
                  }}
                />
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <button className="btn primary sm" onClick={() => { void handleSave(); }} disabled={saving}>
                    {saving ? 'saving…' : 'save'}
                  </button>
                  <button className="btn sm ghost" onClick={() => { setEditing(false); setSaveError(null); }} disabled={saving}>
                    cancel
                  </button>
                  <span className="mono-sm" style={{ color: 'var(--fg-muted)', marginLeft: 'auto' }}>
                    {draft.trim().split(/\s+/).filter(Boolean).length}w
                  </span>
                </div>
                {saveError && (
                  <span style={{ fontSize: 12, color: 'var(--danger, #c0392b)' }}>{saveError}</span>
                )}
              </div>
            ) : (
              <pre style={{ margin: 0, padding: '14px 16px', whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.7, fontFamily: 'var(--font-sans, inherit)' }}>
                {state?.cover_letter ?? 'no cover letter yet — run phase 3'}
              </pre>
            )}
          </div>

          {/* Fit score rationale */}
          {state?.fit_rationale && (
            <div className="card" style={{ padding: '12px 14px' }}>
              <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 6, color: 'var(--fg-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                fit rationale
              </div>
              <p style={{ margin: 0, fontSize: 13, lineHeight: 1.6 }}>{state.fit_rationale}</p>
            </div>
          )}

          {/* Concerns */}
          {state?.fit_concerns && state.fit_concerns.length > 0 && (
            <div className="card" style={{ padding: '12px 14px' }}>
              <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 8, color: 'var(--fg-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                concerns
              </div>
              <ul style={{ margin: 0, paddingLeft: 16, display: 'flex', flexDirection: 'column', gap: 4 }}>
                {state.fit_concerns.map((c, i) => (
                  <li key={i} style={{ fontSize: 12, lineHeight: 1.5, color: 'var(--fg-muted)' }}>{c}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── FactCheckTab ─────────────────────────────────────────────────────────────

interface FactClaim {
  c: string;
  src: string;
  ok: boolean;
}

interface FactCheckTabProps {
  artifacts: ApiApplicationArtifact[];
}

// Extract claims from a fact-check artifact's `output` payload — matches the
// real serialized FactCheckResult shape from src/jobsmith/factcheck.py:
//   { passed: bool,
//     verified_claims: [{ claim, kind, verified, source_file? }],
//     failed_claims:   [str] }
// Returns [] when the artifact is absent OR exposes neither field, in which
// case the tab renders an explicit "no fact-check data yet" empty state.
// Roborev job 945 fix.
function extractFactCheckClaims(artifacts: ApiApplicationArtifact[]): {
  rows: FactClaim[];
  passed: boolean | null;
} {
  const fc = artifacts.find(
    a => a.kind === 'fact-check' || a.kind === 'fact_check' || a.kind === 'factcheck',
  );
  if (!fc) return { rows: [], passed: null };
  const out = fc.output as {
    passed?: unknown;
    verified_claims?: unknown;
    failed_claims?: unknown;
    // Legacy shape kept for forward-compat: some agents may still emit `claims[]`.
    claims?: unknown;
  };
  const rows: FactClaim[] = [];

  // Real shape: verified_claims is a list of VerificationResult dicts.
  if (Array.isArray(out.verified_claims)) {
    for (const item of out.verified_claims as unknown[]) {
      if (typeof item !== 'object' || item === null) continue;
      const obj = item as Record<string, unknown>;
      const claim = typeof obj.claim === 'string' ? obj.claim : null;
      if (!claim) continue;
      const source =
        typeof obj.source_file === 'string' ? obj.source_file
          : typeof obj.source === 'string' ? obj.source
            : '';
      const verified = obj.verified === true || obj.ok === true;
      rows.push({ c: claim, src: source, ok: verified });
    }
  }
  // Real shape: failed_claims is a list of strings (the un-verifiable claim
  // text). They have no source.
  if (Array.isArray(out.failed_claims)) {
    for (const item of out.failed_claims as unknown[]) {
      if (typeof item !== 'string') continue;
      rows.push({ c: item, src: '', ok: false });
    }
  }
  // Legacy: a list of {claim, source, ok}. Only consume if we got nothing
  // from the real fields above (so a real-shape artifact never double-counts).
  if (rows.length === 0 && Array.isArray(out.claims)) {
    for (const item of out.claims as unknown[]) {
      if (typeof item !== 'object' || item === null) continue;
      const obj = item as Record<string, unknown>;
      const claim = typeof obj.claim === 'string' ? obj.claim : null;
      const source = typeof obj.source === 'string' ? obj.source : '';
      const ok = obj.ok === true;
      if (claim) rows.push({ c: claim, src: source, ok });
    }
  }

  const passed = typeof out.passed === 'boolean' ? out.passed : null;
  return { rows, passed };
}

function FactCheckTab({ artifacts }: FactCheckTabProps) {
  const { rows, passed } = extractFactCheckClaims(artifacts);

  if (rows.length === 0) {
    return (
      <div className="card" style={{ padding: 32, textAlign: 'center', color: 'var(--fg-muted)' }}>
        <div className="mono-sm" style={{ marginBottom: 6 }}>no fact-check data yet</div>
        <div style={{ fontSize: 13 }}>
          this run hasn't produced a <code>fact_check.json</code> artifact.
          once the factchecker specialist completes, verified claims will
          appear here.
        </div>
      </div>
    );
  }

  const verifiedCount = rows.filter(r => r.ok).length;
  const summaryBadge =
    passed === false
      ? <Badge kind="danger">{verifiedCount}/{rows.length} verified · failed</Badge>
      : passed === true
        ? <Badge kind="success">{verifiedCount}/{rows.length} verified</Badge>
        : <Badge kind={verifiedCount === rows.length ? 'success' : 'warn'}>
            {verifiedCount}/{rows.length} verified
          </Badge>;

  return (
    <div className="card">
      <div className="card-h">
        <h3>fact-check</h3>
        <span className="sub">cover_draft.md → master/work.yml</span>
        <div className="right">{summaryBadge}</div>
      </div>
      <table className="table">
        <thead>
          <tr><th>claim</th><th>source</th><th>status</th></tr>
        </thead>
        <tbody>
          {rows.map((c, i) => (
            <tr key={i}>
              <td style={{ fontSize: 13 }}>{c.c}</td>
              <td><span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{c.src || '—'}</span></td>
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
  artifacts: ApiApplicationArtifact[];
}

interface AnchorBulletSummary {
  id: string;
  text: string;
}

interface AnchorCheckSummary {
  exitCode: number | null;
  total: number;
  kept: AnchorBulletSummary[];
  droppedWithoutReason: AnchorBulletSummary[];
  droppedWithReason: { bullet: AnchorBulletSummary; reason: string }[];
  message: string | null;
}

// Narrow a JSON-serialized Bullet (jobsmith.guard.Bullet) into the bare
// {id, text} shape we render. Tolerates different id keys (`bullet_id` is
// canonical; some serializers emit `id`).
function bulletSummary(item: unknown): AnchorBulletSummary | null {
  if (typeof item === 'string') return { id: item, text: item };
  if (typeof item !== 'object' || item === null) return null;
  const obj = item as Record<string, unknown>;
  const id =
    (typeof obj.bullet_id === 'string' && obj.bullet_id) ||
    (typeof obj.id === 'string' && obj.id) ||
    (typeof obj.text === 'string' && obj.text.slice(0, 12)) ||
    null;
  if (!id) return null;
  const text = typeof obj.text === 'string' ? obj.text : id;
  return { id, text };
}

// Defensively narrow an anchor-check artifact's output payload. The real
// shape is the JSON serialization of GuardResult from src/jobsmith/guard.py:
//   { exit_code: int,
//     anchor_bullets: [Bullet], kept: [Bullet],
//     dropped_without_reason: [Bullet],
//     dropped_with_reason: [[Bullet, str]] }
// The legacy shape ({preserved: [str], dropped: [str|{id,reason}]}) is also
// accepted as a forward-compat fallback. Roborev job 945 fix.
function extractAnchorSummary(
  artifacts: ApiApplicationArtifact[],
): AnchorCheckSummary | null {
  const a = artifacts.find(x => x.kind === 'anchor-check' || x.kind === 'anchor_check');
  if (!a) return null;
  const out = a.output as {
    exit_code?: unknown;
    anchor_bullets?: unknown;
    kept?: unknown;
    dropped_without_reason?: unknown;
    dropped_with_reason?: unknown;
    message?: unknown;
    // Legacy compat:
    preserved?: unknown;
    dropped?: unknown;
  };

  const exitCode = typeof out.exit_code === 'number' ? out.exit_code : null;
  const message = typeof out.message === 'string' ? out.message : null;

  // Real shape — GuardResult.
  if (
    Array.isArray(out.kept) ||
    Array.isArray(out.dropped_without_reason) ||
    Array.isArray(out.dropped_with_reason) ||
    Array.isArray(out.anchor_bullets)
  ) {
    const kept = (Array.isArray(out.kept) ? out.kept : [])
      .map(bulletSummary)
      .filter((b): b is AnchorBulletSummary => b !== null);
    const droppedWithoutReason = (Array.isArray(out.dropped_without_reason) ? out.dropped_without_reason : [])
      .map(bulletSummary)
      .filter((b): b is AnchorBulletSummary => b !== null);
    const droppedWithReason: { bullet: AnchorBulletSummary; reason: string }[] = [];
    if (Array.isArray(out.dropped_with_reason)) {
      for (const item of out.dropped_with_reason as unknown[]) {
        if (!Array.isArray(item) || item.length < 2) continue;
        const bullet = bulletSummary(item[0]);
        const reason = typeof item[1] === 'string' ? item[1] : '';
        if (bullet) droppedWithReason.push({ bullet, reason });
      }
    }
    const total =
      Array.isArray(out.anchor_bullets)
        ? out.anchor_bullets.length
        : kept.length + droppedWithoutReason.length + droppedWithReason.length;
    return { exitCode, total, kept, droppedWithoutReason, droppedWithReason, message };
  }

  // Legacy shape — {preserved, dropped}.
  if (Array.isArray(out.preserved) || Array.isArray(out.dropped)) {
    const kept = Array.isArray(out.preserved)
      ? (out.preserved as unknown[])
          .filter((s): s is string => typeof s === 'string')
          .map((id) => ({ id, text: id }))
      : [];
    const droppedWithReason: { bullet: AnchorBulletSummary; reason: string }[] = [];
    const droppedWithoutReason: AnchorBulletSummary[] = [];
    if (Array.isArray(out.dropped)) {
      for (const item of out.dropped as unknown[]) {
        if (typeof item === 'string') {
          droppedWithoutReason.push({ id: item, text: item });
        } else if (typeof item === 'object' && item !== null) {
          const obj = item as Record<string, unknown>;
          const id = typeof obj.id === 'string' ? obj.id : null;
          const reason = typeof obj.reason === 'string' ? obj.reason : '';
          if (!id) continue;
          if (reason) droppedWithReason.push({ bullet: { id, text: id }, reason });
          else droppedWithoutReason.push({ id, text: id });
        }
      }
    }
    return {
      exitCode,
      total: kept.length + droppedWithoutReason.length + droppedWithReason.length,
      kept,
      droppedWithoutReason,
      droppedWithReason,
      message,
    };
  }

  // Artifact present but neither shape matches — surface as "no data" rather
  // than fabricate. Caller treats this as an empty result.
  return null;
}

function AnchorCheckTab({ artifacts }: AnchorCheckTabProps) {
  const summary = extractAnchorSummary(artifacts);

  if (!summary) {
    return (
      <div className="card" style={{ padding: 32, textAlign: 'center', color: 'var(--fg-muted)' }}>
        <div className="mono-sm" style={{ marginBottom: 6 }}>no anchor-check data yet</div>
        <div style={{ fontSize: 13 }}>
          this run hasn't produced an <code>anchor_check.json</code> artifact.
        </div>
      </div>
    );
  }

  // exit_code !== 0 OR any anchor dropped without reason → guard failed.
  const guardFailed =
    (summary.exitCode !== null && summary.exitCode !== 0) ||
    summary.droppedWithoutReason.length > 0;

  const summaryBadge =
    summary.total === 0
      ? <Badge>no anchors recorded</Badge>
      : guardFailed
        ? <Badge kind="danger">{summary.kept.length} / {summary.total} preserved · failed</Badge>
        : <Badge kind="success">{summary.kept.length} / {summary.total} preserved</Badge>;

  return (
    <div className="card">
      <div className="card-h">
        <h3>anchor preservation</h3>
        <span className="sub">bullet_selection.json</span>
        <div className="right">{summaryBadge}</div>
      </div>

      {summary.message && (
        <div
          style={{
            margin: '0 16px',
            marginTop: 14,
            padding: '10px 14px',
            background: guardFailed ? 'var(--bg-sunk)' : 'transparent',
            border: guardFailed ? '1px solid var(--danger, var(--border))' : '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            fontSize: 13,
            color: guardFailed ? 'var(--danger, var(--fg))' : 'var(--fg-muted)',
          }}
        >
          {summary.message}
        </div>
      )}

      <div style={{ padding: '18px 20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
        <div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>kept</div>
          {summary.kept.length === 0 ? (
            <div style={{ color: 'var(--fg-subtle)', fontSize: 13 }}>(none)</div>
          ) : summary.kept.map(b => (
            <div key={b.id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0' }}>
              <Icon name="check" size={11} style={{ color: 'var(--success)' }} />
              <span className="mono-sm" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{b.text}</span>
            </div>
          ))}
        </div>
        <div>
          {summary.droppedWithoutReason.length > 0 && (
            <>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--danger, var(--fg-subtle))', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>
                dropped without reason ({summary.droppedWithoutReason.length})
              </div>
              {summary.droppedWithoutReason.map(b => (
                <div key={b.id} style={{ padding: '6px 0', borderLeft: '2px solid var(--danger, var(--border))', paddingLeft: 8 }}>
                  <div className="mono-sm">{b.text}</div>
                </div>
              ))}
            </>
          )}
          {summary.droppedWithReason.length > 0 && (
            <>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10, marginTop: summary.droppedWithoutReason.length > 0 ? 18 : 0 }}>
                dropped with reason ({summary.droppedWithReason.length})
              </div>
              {summary.droppedWithReason.map(({ bullet, reason }) => (
                <div key={bullet.id} style={{ padding: '6px 0' }}>
                  <div className="mono-sm">{bullet.text}</div>
                  {reason && <div style={{ color: 'var(--fg-subtle)', fontSize: 12 }}>{reason}</div>}
                </div>
              ))}
            </>
          )}
          {summary.droppedWithoutReason.length === 0 && summary.droppedWithReason.length === 0 && (
            <>
              <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>dropped</div>
              <div style={{ padding: '24px', textAlign: 'center', color: 'var(--fg-subtle)', background: 'var(--bg-sunk)', borderRadius: 'var(--radius)', border: '1px dashed var(--border)' }}>
                <div className="mono-sm">none</div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── ConfigTab ────────────────────────────────────────────────────────────────

interface ConfigTabProps {
  app: SampleApp;
}

function ConfigTab({ app }: ConfigTabProps) {
  // The .apply-config.yaml panel previously displayed a hardcoded YAML
  // body (author: jordan-smith, phase_timeout_s: 600, etc.) regardless of
  // the user's actual config. Removed pending an /api/applications/{slug}/config
  // endpoint; the existing /api/config view (Config page) is the authoritative
  // source for the global config in the meantime.
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div className="card">
        <div className="card-h"><h3>.apply-config.yaml</h3></div>
        <div style={{ padding: '24px 18px', color: 'var(--fg-muted)', fontSize: 13 }}>
          per-application config view not yet wired. visit the global{' '}
          <span className="mono-sm">Config</span> page in the sidebar to inspect
          <span className="mono-sm"> .apply-config.yaml</span>.
        </div>
      </div>
      <div className="card">
        <div className="card-h"><h3>run options</h3></div>
        <div style={{ padding: '16px 18px' }}>
          <div className="field"><label>job url</label><input className="mono" defaultValue={app.url} readOnly /></div>
          <div className="field"><label>slug</label><input className="mono" defaultValue={app.slug} readOnly /></div>
          <div style={{ fontSize: 12, color: 'var(--fg-subtle)', marginTop: 8 }}>
            run options are read-only here; use <span className="mono-sm">re-run apply</span> on the
            page header to launch a new run.
          </div>
        </div>
      </div>
    </div>
  );
}

// ── ApplicationDetail (top-level export) ─────────────────────────────────────

/**
 * Synthesise a `SampleApp`-shaped object from the API row so the existing
 * presentational subtree (PhaseCard / PipelineTab / ArtifactsTab / etc.)
 * keeps working. `role`/`company` come from the API; anchors/factcheck/renders
 * remain placeholders until the API surfaces them.
 */
function fromApi(slug: string, api: ApiApplicationDetail | undefined): SampleApp {
  const phaseStr = api?.phase ?? '';
  // Map the API phase string to a numeric phase. Only the three real phase
  // names produce a numbered value. rendered/done imply phase 3 completed.
  // Any other value — including 'unknown' (the full-pipeline marker written
  // by apply.py:_run_apply._db_phase_label) — returns null so downstream
  // consumers (deriveProgress) leave all phase cards in their default queued
  // state instead of falsely attributing the failure to phase 1.
  // Closes bug-300fb9ad (roborev job 968 MEDIUM).
  const phaseNum: AppPhase =
    phaseStr === 'gather' ? 1 :
    phaseStr === 'draft' ? 2 :
    phaseStr === 'render' ? 3 :
    api?.status === 'rendered' ? 3 :
    api?.status === 'done' ? 3 :
    api?.status === 'incomplete' ? 2 :
    null;
  const updatedAt = api?.finished_at ?? api?.started_at ?? null;
  return {
    slug,
    role: api?.role ?? '—',
    company: api?.company ?? '—',
    status: (api?.status ?? 'queued') as AppStatus,
    updated: updatedAt ? new Date(updatedAt).toLocaleString() : '—',
    phase: phaseNum,
    anchors: '—',
    factcheck: '—',
    renders: [],
    // Prefer the new `apply_url` field (feat-bb81c3ce, extracted from the
    // jd-parsed artifact by the backend). Fall back to the legacy `url` field
    // on ApplicationRow for older rows that pre-date apply_url persistence.
    // Empty string when neither is available — `hasLaunchableUrl` will be
    // false and the re-run button stays disabled.
    url: api?.apply_url ?? api?.url ?? '',
  };
}

// ── SSE event shapes ─────────────────────────────────────────────────────

interface SsePhaseEvent {
  run_id: string;
  phase: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
}

interface SseLogEvent {
  run_id: string;
  stream: string;
  line: string;
  timestamp: string | null;
}

interface SseSpecialistEvent {
  run_id: string;
  specialist: string;
  kind: string;
  kind_label: string;
  phase: string;
  status: string;
  finished_at: string | null;
}

// Map a phase string from the SSE event to a 1|2|3 number.
function ssePhaseToNum(phase: string): 1 | 2 | 3 {
  if (phase === 'gather') return 1;
  if (phase === 'draft') return 2;
  if (phase === 'render') return 3;
  return 1;
}

/**
 * Map a specialist name to its phase number. Mirrors the canonical
 * ``PHASE_SPECIALISTS`` map in ``src/jobsmith/_state_readers.py``.
 *
 * Returns null when the name is not a known specialist — the caller
 * decides whether to fall back to the SSE payload's phase label or
 * drop the event. Used by the specialist SSE handler so a forwarded
 * ``apply_runs.phase="unknown"`` does not corrupt phase progress.
 */
function specialistToPhaseNum(specialist: string): 1 | 2 | 3 | null {
  const gatherSpecs = new Set([
    'apply-jd-parser',
    'apply-fit-scorer',
    'apply-hm-enricher',
    'apply-bullet-selector',
    'apply-company-research',
  ]);
  const draftSpecs = new Set(['apply-prose-writer', 'apply-prose-qa']);
  const renderSpecs = new Set([
    'apply-resume-renderer',
    'apply-portfolio-ats-checker',
    'apply-visual-layout-reviewer',
    'apply-cover-letter-writer',
    'apply-index-writer',
  ]);
  if (gatherSpecs.has(specialist)) return 1;
  if (draftSpecs.has(specialist)) return 2;
  if (renderSpecs.has(specialist)) return 3;
  return null;
}

function specialistEventStatus(status: string): SpecialistRunStatus {
  if (status === 'failed') return 'failed';
  if (status === 'done' || status === 'backfilled' || status === 'complete') return 'done';
  if (status === 'running' || status === 'started') return 'running';
  return 'done';
}

export function ApplicationDetail({ slug, back }: ApplicationDetailProps) {
  const { data: apiDetail, isLoading, error } = useApplication(slug);

  // Proposal context — auto-switch to Review tab + refresh after apply.
  const { pendingProposal, appliedVersion } = useProposal();

  const app = useMemo<SampleApp>(() => fromApi(slug, apiDetail), [slug, apiDetail]);
  const {
    progress: initialProgress,
    activePhase: initialActivePhase,
    failedPhaseNum: initialFailedPhaseNum,
  } = deriveProgress(app);

  const [tab, setTab] = useState<TabName>('pipeline');
  // reviewKey is bumped on successful apply so ReviewTab re-fetches.
  const [reviewKey, setReviewKey] = useState(0);
  const [activePhase, setActivePhase] = useState<number>(initialActivePhase);
  const [running, setRunning] = useState<boolean>(app.status === 'running');
  const [progress, setProgress] = useState<ProgressMap>(initialProgress);
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [runError, setRunError] = useState<string | null>(null);
  // SSE-derived terminal status — overrides app.status in the header badge when
  // a phase event with status=failed arrives over the stream. Null means no SSE
  // terminal event has been received yet; fall back to app.status in that case.
  // For an already-failed app loaded from the API, seed this so the header
  // badge does not flip from 'failed' back to the running default during
  // initial render (closes roborev job 965 MEDIUM).
  const [sseStatus, setSseStatus] = useState<AppStatus | null>(
    app.status === 'failed' ? 'failed' : null,
  );
  // Phase number (1=gather, 2=draft, 3=render) that the SSE failure event
  // pinned the failure to. Used by PipelineTab so a gather failure does not
  // mis-render the queued draft + render specialists as "failed" (closes
  // roborev job 953 LOW). Seeded from ``deriveProgress`` so reloading a
  // failed run shows which phase failed without waiting for a live SSE
  // event (closes roborev job 965 MEDIUM).
  const [failedPhaseNum, setFailedPhaseNum] = useState<1 | 2 | 3 | null>(
    initialFailedPhaseNum,
  );
  const [specialistStatuses, setSpecialistStatuses] = useState<SpecialistStatusMap>({});

  // Ref to hold the active EventSource so we can close it on cancel/unmount.
  const esRef = useRef<EventSource | null>(null);

  // Roborev job 967 MEDIUM: ``apiDetail`` arrives asynchronously after
  // the first render, so the ``useState`` initializers above run
  // against the placeholder app and never re-run when the real data
  // lands. Sync ``progress`` / ``activePhase`` / ``failedPhaseNum`` /
  // ``sseStatus`` / ``running`` when ``apiDetail`` loads or refreshes
  // so reloading a failed application paints the right phase card
  // immediately, without waiting for a live SSE event.
  useEffect(() => {
    if (apiDetail === undefined) return;
    const next = deriveProgress(app);
    const fromArtifacts: SpecialistStatusMap = {};
    for (const artifact of apiDetail.artifacts ?? []) {
      if (artifact.specialist) fromArtifacts[artifact.specialist] = 'done';
    }
    const mergedProgress = mergeArtifactProgress(apiDetail, next.progress);
    setProgress(mergedProgress);
    if (app.status === 'running') {
      const firstIncomplete = ([1, 2, 3] as const).find(n => mergedProgress[n] < 100);
      setActivePhase(firstIncomplete ?? next.activePhase);
    } else {
      setActivePhase(next.activePhase);
    }
    setFailedPhaseNum(next.failedPhaseNum);
    if (app.status === 'failed') {
      setSseStatus('failed');
      setRunning(false);
    } else if (app.status === 'rendered' || app.status === 'done') {
      setSseStatus('done');
      setRunning(false);
    } else if (app.status === 'running') {
      setRunning(true);
    }
    setSpecialistStatuses(fromArtifacts);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiDetail]);

  // Auto-switch to the Review tab when a proposal arrives for this slug.
  useEffect(() => {
    if (pendingProposal?.proposal.slug === slug && tab !== 'review') {
      setTab('review');
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingProposal, slug]);

  // Bump reviewKey after a successful apply so ReviewTab re-fetches.
  useEffect(() => {
    if (appliedVersion > 0) {
      setReviewKey((k) => k + 1);
    }
  }, [appliedVersion]);

  // ── Subscribe to SSE stream ──────────────────────────────────────────
  const subscribeToEvents = useCallback((targetSlug: string) => {
    // Close any existing connection.
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }

    const url = buildEventsUrl(targetSlug);
    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener('phase', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data as string) as SsePhaseEvent;
        const phaseNum = ssePhaseToNum(data.phase);
        if (data.status === 'done' || data.status === 'backfilled') {
          setProgress(p => ({ ...p, [phaseNum]: 100 }));
          // Only mark the whole run done when the last phase (render=3) completes.
          // Also flip the running flag — without this, the header badge keeps
          // showing "running" because `running ? 'running' : ...` takes
          // precedence over sseStatus (roborev job 948 MEDIUM).
          if (phaseNum === 3) {
            setRunning(false);
            setSseStatus('done');
          }
        } else if (data.status === 'running') {
          setProgress(p => ({ ...p, [phaseNum]: Math.max(p[phaseNum], 10) }));
          setActivePhase(phaseNum);
          setRunning(true);
          setSseStatus('running');
        } else if (data.status === 'failed') {
          setRunning(false);
          setSseStatus('failed');
          // roborev job 957 MEDIUM: when the API's apply_runs row is
          // marked failed it carries phase="unknown" (full-pipeline
          // marker — see apply.py:_run_apply._db_phase_label).
          // ``ssePhaseToNum("unknown")`` defaults to 1, so blindly
          // setting failedPhaseNum here would overwrite a draft/render
          // failure that was correctly pinned by an earlier transcript
          // ``phase_failed`` event with a real phase name. Only update
          // when the SSE payload names a real phase.
          if (data.phase === 'gather' || data.phase === 'draft' || data.phase === 'render') {
            setFailedPhaseNum(phaseNum as 1 | 2 | 3);
          }
        }
        notifyDataChanged(`/api/applications/${targetSlug}`);
        // Add a log entry for the phase event. Backend resends phase state on
        // every SSE poll, so dedup identical phase+status against the most
        // recent phase row and cap the list (mirrors specialist/log handlers).
        const msg = `&lt;&lt;PHASE&gt;&gt; ${data.phase} status=${data.status}`;
        setEvents(ev => {
          if (ev.length > 400) return ev;
          for (let i = ev.length - 1; i >= 0; i--) {
            if (ev[i].lvl === 'done' && ev[i].msg?.startsWith('&lt;&lt;PHASE&gt;&gt;')) {
              if (ev[i].msg === msg) return ev;
              break;
            }
          }
          return [...ev, { ts: now(), lvl: 'done', msg }];
        });
      } catch { /* ignore malformed event */ }
    });

    es.addEventListener('specialist', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data as string) as SseSpecialistEvent;
        const specialist = redactSensitive(String(data.specialist ?? ''));
        const kindLabel = redactSensitive(String(data.kind_label ?? ''));
        const msg = `<span class="dim">specialist=</span>${specialist} <span class="dim">kind=</span>${kindLabel}`;
        setEvents(ev => ev.length > 400 ? ev : [...ev, { ts: now(), lvl: 'spec', msg }]);
        if (specialist) {
          setSpecialistStatuses(prev => ({
            ...prev,
            [specialist]: specialistEventStatus(String(data.status ?? 'done')),
          }));
          notifyDataChanged(`/api/applications/${targetSlug}`);
        }
        // Advance phase bar progress incrementally for each specialist.
        // Roborev job 961 MEDIUM: ``events_poll`` forwards
        // ``apply_runs.phase`` on specialist events, but apply.py records
        // the row's phase as the full-pipeline label ``"unknown"``.
        // ``ssePhaseToNum('unknown')`` defaults to 1, so a draft- or
        // render-phase specialist event would bump phase 1 progress
        // (and could regress a completed phase 1 from 100 back down).
        // Map specialist NAME → phase via the canonical lookup we
        // already use server-side, falling back to the SSE phase only
        // when it names a real phase. ``unknown`` is dropped.
        const phaseNum = specialistToPhaseNum(specialist) ?? (
          (data.phase === 'gather' || data.phase === 'draft' || data.phase === 'render')
            ? ssePhaseToNum(data.phase)
            : null
        );
        if (phaseNum !== null) {
          // Roborev job 964 MEDIUM: progress must be monotonic. The
          // raw ``Math.min(90, p[phaseNum] + 15)`` caps at 90, so a
          // late-arriving specialist event for a phase already at 100
          // (because the matching ``phase done`` event landed first)
          // would regress the bar back to 90 and the UI would show
          // the completed phase as queued/incomplete. ``Math.max``
          // against the prior value pins the floor at "what was".
          setProgress(p => ({
            ...p,
            [phaseNum]: Math.max(
              p[phaseNum],
              Math.min(90, p[phaseNum] + 15),
            ),
          }));
        }
      } catch { /* ignore malformed event */ }
    });

    es.addEventListener('log', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data as string) as SseLogEvent;
        // Redact tokens BEFORE HTML escaping so the redaction matches the raw
        // server-emitted string (which may contain `?token=…` from request URLs
        // logged to stderr).
        const safe = redactSensitive(data.line);
        const line = safe.replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const lvl = data.stream === 'stderr' ? 'warn' : 'info';
        setEvents(ev => ev.length > 400 ? ev : [...ev, { ts: now(), lvl, msg: line, kind: 'log' }]);
      } catch { /* ignore malformed event */ }
    });

    // bug-0e13706c: structured agent events tailed from transcript.jsonl.
    // Rendered as typed rows (tool_call / tool_result / agent_text /
    // phase_boundary) instead of pre-formatted terminal log lines.
    es.addEventListener('transcript', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data as string) as SseTranscriptEvent;
        const ev = transcriptToLogEvent(data);
        if (ev === null) return;
        // Redact + escape any string fields that touch the DOM.
        if (ev.msg) ev.msg = redactSensitive(ev.msg).replace(/</g, '&lt;').replace(/>/g, '&gt;');
        if (ev.toolInputPreview) ev.toolInputPreview = redactSensitive(ev.toolInputPreview);
        setEvents(prev => prev.length > 400 ? prev : [...prev, ev]);
      } catch { /* ignore malformed event */ }
    });

    es.addEventListener('idle-close', () => {
      setRunning(false);
      es.close();
      esRef.current = null;
    });

    es.onerror = () => {
      // EventSource will auto-reconnect; mark not running if it was a terminal error.
      // We only stop running if the connection fails immediately (readyState CLOSED).
      if (es.readyState === EventSource.CLOSED) {
        setRunning(false);
      }
    };
  }, []);

  // While a run is active, keep the detail query fresh even if the SSE stream
  // only carries transcript rows. This lets artifact-derived UI state catch up
  // for failed phases that wrote files but never reached post-phase DB ingest.
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => {
      notifyDataChanged(`/api/applications/${slug}`);
    }, 3000);
    return () => window.clearInterval(id);
  }, [running, slug]);

  // A slug is "complete" when its latest run finished successfully or was
  // backfilled. Re-running such a slug requires --force on the server side or
  // the apply pipeline aborts with "Application already complete." (GH#50).
  const isComplete =
    app.status === 'done' ||
    app.status === 'rendered' ||
    (app.status as string) === 'backfilled';

  // The detail API does not currently expose the original job URL — fromApi()
  // sets `url: ''` unconditionally. Without a real URL we cannot launch a
  // valid apply run; in particular, sending a placeholder with `force=true`
  // would destructively restart the pipeline against a fake URL and
  // overwrite real artifacts (roborev job 944). So: when no URL is
  // available, the re-run button is disabled and the user is pointed at the
  // CLI. Re-enable here once /api/applications/{slug} returns a `url` field.
  const hasLaunchableUrl = Boolean(app.url);
  const apiStatus = apiDetail?.status;
  const apiRunId = apiDetail?.run_id;

  // ── Re-run apply handler (real API) ──────────────────────────────────
  const handleReRun = useCallback(async () => {
    if (!hasLaunchableUrl) {
      setRunError(
        'this slug has no recorded job URL on the server — use ' +
        '`jobsmith apply --force <url>` from the CLI to re-run.',
      );
      return;
    }

    setRunError(null);
    // Reset state for a fresh run.
    setProgress({ 1: 0, 2: 0, 3: 0 });
    setActivePhase(1);
    setSseStatus(null);
    setFailedPhaseNum(null);
    setSpecialistStatuses({});
    setEvents([{ ts: now(), lvl: 'info', msg: `<span class="dim">apply</span> start <span class="dim">slug=</span>${slug}` }]);
    setRunning(true);

    try {
      // Pass force=true when re-running an already-complete slug. Without it
      // the apply pipeline silently aborts after the supervisor returns 201.
      await postApplication(app.url, slug, { force: isComplete });
      subscribeToEvents(slug);
    } catch (err) {
      setRunning(false);
      const raw = err instanceof JobsmithApiError ? err.message : String(err);
      const msg = redactSensitive(raw);
      setRunError(msg);
      // Re-add a failed event.
      setEvents(ev => [...ev, { ts: now(), lvl: 'warn', msg: `launch failed: ${msg}` }]);
    }
  }, [slug, app.url, isComplete, hasLaunchableUrl, subscribeToEvents]);

  // ── Run render phase only ────────────────────────────────────────────
  const handleRunRender = useCallback(async () => {
    if (!hasLaunchableUrl) {
      setRunError(
        'this slug has no recorded job URL — use ' +
        '`jobsmith apply --force <url>` from the CLI.',
      );
      return;
    }
    setRunError(null);
    setProgress({ 1: 100, 2: 100, 3: 0 });
    setActivePhase(3);
    setSseStatus(null);
    setFailedPhaseNum(null);
    setSpecialistStatuses({});
    setEvents([{ ts: now(), lvl: 'info', msg: `<span class="dim">apply</span> render <span class="dim">slug=</span>${slug}` }]);
    setRunning(true);
    try {
      await postApplication(app.url, slug, { startFromPhase: 'render' });
      subscribeToEvents(slug);
    } catch (err) {
      setRunning(false);
      const raw = err instanceof JobsmithApiError ? err.message : String(err);
      const msg = redactSensitive(raw);
      setRunError(msg);
      setEvents(ev => [...ev, { ts: now(), lvl: 'warn', msg: `launch failed: ${msg}` }]);
    }
  }, [slug, app.url, hasLaunchableUrl, subscribeToEvents]);

  // ── Cancel handler ───────────────────────────────────────────────────
  const handleCancel = useCallback(async () => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    try {
      if (apiRunId) {
        await apiPost(`/api/applications/${slug}/runs/${apiRunId}/cancel`, {});
      }
      setRunning(false);
      setEvents(ev => [...ev, { ts: now(), lvl: 'warn', msg: 'run cancelled by user' }]);
    } catch (err) {
      const msg = redactSensitive(err instanceof Error ? err.message : String(err));
      setRunError(msg);
      setEvents(ev => [...ev, { ts: now(), lvl: 'warn', msg: `cancel failed: ${msg}` }]);
    }
  }, [apiRunId, slug]);

  // Close SSE on unmount.
  useEffect(() => {
    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, []);

  // Auto-subscribe to the SSE stream when we open a slug whose latest run
  // is currently running. Without this, navigating to an in-flight
  // application leaves the event log + progress bar frozen until the user
  // clicks "re-run apply" — which is exactly the bug GH#52 reported.
  // The subscription is keyed by slug + apiDetail.run_id so a true new run
  // (e.g. user clicks force re-run, server returns a new run_id) drops the
  // stale stream and resubscribes; cleanup runs on slug change or unmount.
  // Roborev job 945 fix.
  useEffect(() => {
    if (apiStatus !== 'running') return;
    if (!apiRunId) return;
    subscribeToEvents(slug);
    setRunning(true);
    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, [slug, apiRunId, apiStatus, subscribeToEvents]);

  // Seed the event log from the stored transcript for completed/failed runs.
  // For active runs, the live SSE stream populates events; replaying the
  // transcript would duplicate entries. Only fires when the run is done.
  useEffect(() => {
    if (!apiDetail) return;
    if (apiStatus === 'running') return;
    let cancelled = false;
    async function loadTranscript() {
      try {
        const rows = await apiGet<Array<{ run_id: string; payload: Record<string, unknown> }>>(
          `/api/applications/${slug}/transcript`,
        );
        if (cancelled) return;
        const logEvents: LogEvent[] = [];
        for (const row of rows) {
          const ev = transcriptToLogEvent({ run_id: row.run_id, payload: row.payload });
          if (ev) logEvents.push(ev);
        }
        if (logEvents.length > 0) {
          setEvents(logEvents);
        }
      } catch (err) {
        const msg = redactSensitive(err instanceof Error ? err.message : String(err));
        setEvents(ev => ev.length > 0 ? ev : [
          { ts: now(), lvl: 'warn', msg: `transcript unavailable: ${msg}` },
        ]);
      }
    }
    void loadTranscript();
    return () => { cancelled = true; };
  // Only re-run when the slug or run_id changes — not on every render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, apiRunId]);

  // Mark not-running when all phases reach 100%.
  const allDone = progress[1] >= 100 && progress[2] >= 100 && progress[3] >= 100;
  useEffect(() => {
    if (allDone) {
      setRunning(false);
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    }
  }, [allDone]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || target?.isContentEditable) return;
      if (e.key === 'Escape') {
        e.preventDefault();
        back();
        return;
      }
      const idx = Number(e.key);
      if (Number.isInteger(idx) && idx >= 1 && idx <= TAB_ORDER.length) {
        e.preventDefault();
        setTab(TAB_ORDER[idx - 1]);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [back]);

  if (isLoading || error) {
    return (
      <div className="content wide">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <button className="btn ghost sm" onClick={back}>
            <Icon name="arrow" size={12} style={{ transform: 'scaleX(-1)' }} /> applications
          </button>
        </div>
        <div className="card" style={{ padding: 32, textAlign: 'center', color: 'var(--fg-muted)' }}>
          {isLoading && <span>loading <span className="mono">{slug}</span>…</span>}
          {error && (error instanceof JobsmithApiError && error.status === 401 ? (
            <div>
              <div style={{ marginBottom: 8, color: 'var(--danger, #c0392b)' }}>
                API requires <span className="mono">VITE_JOBSMITH_API_TOKEN</span>.
              </div>
              <div className="mono-sm">
                copy from <code>&lt;project&gt;/private/jobsmith.token</code> to <code>web/.env.local</code>, then restart <code>npm run dev</code>.
              </div>
            </div>
          ) : (
            <span>failed to load application: {error.message}</span>
          ))}
        </div>
      </div>
    );
  }

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
            <h1 style={{ margin: 0 }}>{app.role}</h1>
            <StatusBadge status={running ? 'running' : (sseStatus ?? app.status)} />
          </div>
          <div style={{ display: 'flex', gap: 14, marginTop: 8, color: 'var(--fg-muted)', fontSize: 13 }}>
            <span>{app.company}</span>
            <span style={{ color: 'var(--fg-subtle)' }}>·</span>
            <span className="mono-sm">{app.slug}</span>
            <span style={{ color: 'var(--fg-subtle)' }}>·</span>
            <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>updated {app.updated}</span>
          </div>
        </div>
        <div className="actions">
          <button className="btn" onClick={() => {
            void apiPost(`/api/applications/${slug}/reveal`, {}).catch(console.error);
          }}><Icon name="folder" size={13} /> reveal in finder</button>
          {!running && (app.status as string) === 'incomplete' && (
            <button
              className="btn primary"
              onClick={() => { void handleRunRender(); }}
              disabled={!hasLaunchableUrl}
              title="Run the render phase (phase 3) only — gather and draft are already complete."
            >
              <Icon name="play" size={12} /> run render phase
            </button>
          )}
          {!running && (app.status as string) !== 'incomplete' && (
            <button
              className="btn primary"
              onClick={() => { void handleReRun(); }}
              disabled={!hasLaunchableUrl}
              title={
                !hasLaunchableUrl
                  ? 'This slug has no recorded job URL on the server. Use `jobsmith apply --force <url>` from the CLI to re-run.'
                  : isComplete
                  ? 'This slug already completed. Clicking will overwrite the existing run artifacts (--force).'
                  : 'Launch a new apply run for this slug.'
              }
            >
              <Icon name="play" size={12} /> {isComplete ? 'force re-run apply' : 're-run apply'}
            </button>
          )}
          {running && (
            <button className="btn danger" onClick={() => { void handleCancel(); }}>
              <Icon name="x" size={12} /> cancel run
            </button>
          )}
          {runError && (
            <span style={{ fontSize: 12, color: 'var(--danger, #c0392b)', maxWidth: 260 }}>
              {runError}
            </span>
          )}
        </div>
      </div>

      <div className="pipeline" style={{ marginBottom: 20 }}>
        {(() => {
          const firstIncomplete = ([1, 2, 3] as const).findIndex(n => progress[n] < 100);
          return PHASES.map((p, i) => {
          const pr = progress[p.num];
          // bug-8ade6f70 + roborev job 955 MEDIUM: pin the failed-phase
          // marker on the SSE failure payload's phase number rather than
          // ``firstIncomplete``. If a draft/render failure follows a
          // missed/truncated completion event for the prior phase, the
          // earlier ``firstIncomplete`` heuristic would mis-paint the
          // already-finished phase as failed. ``failedPhaseNum`` is the
          // authoritative source.
          const isStuckPhase =
            sseStatus === 'failed' && failedPhaseNum === p.num;
          const status: PhaseStatus = pr >= 100
            ? 'done'
            : (isStuckPhase
              ? 'failed'
              : (running && i === firstIncomplete ? 'running' : 'queued'));
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
          });
        })()}
      </div>

      <div className="tabs">
        {TAB_ORDER.map(t => (
          <div key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>{t}</div>
        ))}
      </div>

      {tab === 'pipeline' && <PipelineTab events={events} running={running} phase={activePhase} progress={progress} sseStatus={sseStatus} failedPhaseNum={failedPhaseNum} specialistStatuses={specialistStatuses} />}
      {tab === 'review' && <ReviewTab slug={slug} reviewKey={reviewKey} />}
      {tab === 'documents' && <DocumentsTab slug={slug} />}
      {tab === 'artifacts' && <ArtifactsTab artifacts={apiDetail?.artifacts ?? []} />}
      {tab === 'factcheck' && <FactCheckTab artifacts={apiDetail?.artifacts ?? []} />}
      {tab === 'anchors' && <AnchorCheckTab artifacts={apiDetail?.artifacts ?? []} />}
      {tab === 'config' && <ConfigTab app={app} />}
    </div>
  );
}
