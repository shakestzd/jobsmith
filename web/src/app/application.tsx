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
import { JobsmithApiError, postApplication, buildEventsUrl, redactSensitive } from '../api/client';
import type {
  ApplicationDetail as ApiApplicationDetail,
  ApplicationArtifact as ApiApplicationArtifact,
} from '../api/types';

// ── Public prop type ─────────────────────────────────────────────────────────

export interface ApplicationDetailProps {
  /** The application slug to display. Falls back to SAMPLE_APPS[0] if not found. */
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

// The event log starts empty — entries are appended ONLY from the real SSE
// stream subscribed in subscribeToEvents(). Previously this seeded a 15-line
// fabricated event sequence (see GH#52); rendering those alongside live SSE
// data made the UI lie about what the pipeline was doing.

// ── Tab type ─────────────────────────────────────────────────────────────────

type TabName = 'pipeline' | 'artifacts' | 'factcheck' | 'anchors' | 'config';

// ── Progress map type ────────────────────────────────────────────────────────

type ProgressMap = Record<1 | 2 | 3, number>;

// ── Progress derivation helper ───────────────────────────────────────────────

function deriveProgress(app: SampleApp): {
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
    const failedPhase = (app.phase || 1) as 1 | 2 | 3;
    const progress: ProgressMap = { 1: 0, 2: 0, 3: 0 };
    // Mark all phases up to and including the failed phase as we got there
    if (failedPhase >= 1) progress[1] = 100;
    if (failedPhase >= 2) progress[2] = 100;
    if (failedPhase >= 3) progress[3] = 100;
    return { progress, activePhase: failedPhase };
  }

  // For running statuses: initialize based on phase
  // Phase 0 (queued) starts at nothing
  if (app.phase === 0) {
    return {
      progress: { 1: 0, 2: 0, 3: 0 },
      activePhase: 1,
    };
  }

  // Phase 1 (gather) running or gather status
  if (app.phase === 1 || app.status === 'gather') {
    return {
      progress: { 1: app.status === 'running' ? 42 : 0, 2: 0, 3: 0 },
      activePhase: 1,
    };
  }

  // Phase 2 (draft) running or draft status
  if (app.phase === 2 || app.status === 'draft') {
    return {
      progress: { 1: 100, 2: app.status === 'running' ? 42 : 0, 3: 0 },
      activePhase: 2,
    };
  }

  // Phase 3 (render) running or review status
  if (app.phase === 3 || app.status === 'review') {
    return {
      progress: { 1: 100, 2: 100, 3: app.status === 'running' ? 42 : 0 },
      activePhase: 3,
    };
  }

  // Default fallback (should not reach here)
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
  events: LogEvent[];
  running: boolean;
  phase: number;
  progress: ProgressMap;
}

function PipelineTab({ events, running, phase, progress }: PipelineTabProps) {
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
          {events.map((e, i) => (
            <div key={i}>
              <span className="ts">{e.ts}</span>
              <span className={`lvl ${e.lvl}`}>{e.lvl.padEnd(6)}</span>
              <span className="msg" dangerouslySetInnerHTML={{ __html: e.msg }} />
            </div>
          ))}
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
              const pct = phaseDone ? 100 : Math.min(100, progress[phaseNum] * (1 + Math.random() * 0.4));
              void pct; // computed but used only implicitly via done/running/queued label
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

// PdfPreview was removed in feat-83d6cf54 (GH#52). It previously rendered a
// hardcoded resume mockup ("Recurly Engineering · Senior Engineer",
// "11m → 2m20s deploy time", "$140k/yr recovered", etc.) regardless of the
// real master/work.yml content. The new ArtifactsTab renders artifact JSON
// directly; rendered-PDF preview will return when an API endpoint serves
// the bytes.

// ── FactCheckTab ─────────────────────────────────────────────────────────────

interface FactClaim {
  c: string;
  src: string;
  ok: boolean;
}

interface FactCheckTabProps {
  artifacts: ApiApplicationArtifact[];
}

// Extract claims from a fact-check artifact's `output` payload. The shape
// is intentionally loose (Record<string, unknown>) on the API side, so we
// defensively narrow it here. Returns [] when no fact-check artifact exists
// or its shape doesn't match — the empty state renders an explicit "no
// fact-check data yet" message rather than fabricating claims.
function extractFactCheckClaims(artifacts: ApiApplicationArtifact[]): FactClaim[] {
  const fc = artifacts.find(
    a => a.kind === 'fact-check' || a.kind === 'fact_check' || a.kind === 'factcheck',
  );
  if (!fc) return [];
  const raw = (fc.output as { claims?: unknown }).claims;
  if (!Array.isArray(raw)) return [];
  const out: FactClaim[] = [];
  for (const item of raw) {
    if (typeof item !== 'object' || item === null) continue;
    const obj = item as Record<string, unknown>;
    const claim = typeof obj.claim === 'string' ? obj.claim : null;
    const source = typeof obj.source === 'string' ? obj.source : '';
    const ok = obj.ok === true;
    if (claim) out.push({ c: claim, src: source, ok });
  }
  return out;
}

function FactCheckTab({ artifacts }: FactCheckTabProps) {
  const claims = extractFactCheckClaims(artifacts);

  if (claims.length === 0) {
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

  return (
    <div className="card">
      <div className="card-h">
        <h3>fact-check</h3>
        <span className="sub">cover_draft.md → master/work.yml</span>
        <div className="right">
          <Badge kind="success">{claims.filter(c => c.ok).length}/{claims.length} verified</Badge>
        </div>
      </div>
      <table className="table">
        <thead>
          <tr><th>claim</th><th>source</th><th>status</th></tr>
        </thead>
        <tbody>
          {claims.map((c, i) => (
            <tr key={i}>
              <td style={{ fontSize: 13 }}>{c.c}</td>
              <td><span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{c.src}</span></td>
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

interface AnchorCheckSummary {
  preserved: string[];
  dropped: { id: string; reason: string }[];
}

// Defensively narrow an anchor-check artifact's output payload. Loose
// shape on the wire — empty result if absent or malformed.
function extractAnchorSummary(
  artifacts: ApiApplicationArtifact[],
): AnchorCheckSummary | null {
  const a = artifacts.find(x => x.kind === 'anchor-check' || x.kind === 'anchor_check');
  if (!a) return null;
  const out = a.output as {
    preserved?: unknown;
    dropped?: unknown;
  };
  const preserved = Array.isArray(out.preserved)
    ? (out.preserved as unknown[]).filter((s): s is string => typeof s === 'string')
    : [];
  const dropped: { id: string; reason: string }[] = [];
  if (Array.isArray(out.dropped)) {
    for (const item of out.dropped as unknown[]) {
      if (typeof item === 'string') {
        dropped.push({ id: item, reason: '' });
      } else if (typeof item === 'object' && item !== null) {
        const obj = item as Record<string, unknown>;
        const id = typeof obj.id === 'string' ? obj.id : null;
        const reason = typeof obj.reason === 'string' ? obj.reason : '';
        if (id) dropped.push({ id, reason });
      }
    }
  }
  return { preserved, dropped };
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

  const total = summary.preserved.length + summary.dropped.length;
  const preservedBadge =
    total === 0
      ? <Badge kind="default">no anchors recorded</Badge>
      : <Badge kind={summary.dropped.length === 0 ? 'success' : 'warn'}>
          {summary.preserved.length} / {total} preserved
        </Badge>;

  return (
    <div className="card">
      <div className="card-h">
        <h3>anchor preservation</h3>
        <span className="sub">bullet_selection.json</span>
        <div className="right">{preservedBadge}</div>
      </div>
      <div style={{ padding: '18px 20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
        <div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>preserved anchors</div>
          {summary.preserved.length === 0 ? (
            <div style={{ color: 'var(--fg-subtle)', fontSize: 13 }}>(none)</div>
          ) : summary.preserved.map(id => (
            <div key={id} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0' }}>
              <Icon name="check" size={11} style={{ color: 'var(--success)' }} />
              <span className="mono-sm">{id}</span>
            </div>
          ))}
        </div>
        <div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>dropped (with reasons)</div>
          {summary.dropped.length === 0 ? (
            <div style={{ padding: '24px', textAlign: 'center', color: 'var(--fg-subtle)', background: 'var(--bg-sunk)', borderRadius: 'var(--radius)', border: '1px dashed var(--border)' }}>
              <div className="mono-sm">none</div>
            </div>
          ) : summary.dropped.map(d => (
            <div key={d.id} style={{ padding: '6px 0' }}>
              <div className="mono-sm">{d.id}</div>
              {d.reason && <div style={{ color: 'var(--fg-subtle)', fontSize: 12 }}>{d.reason}</div>}
            </div>
          ))}
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
  const phaseNum: AppPhase =
    phaseStr === 'gather' ? 1 :
    phaseStr === 'draft' ? 2 :
    phaseStr === 'render' ? 3 :
    api?.status === 'rendered' ? 3 :
    api?.status === 'done' ? 3 :
    1;
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
    url: '',
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

export function ApplicationDetail({ slug, back }: ApplicationDetailProps) {
  const { data: apiDetail, isLoading, error } = useApplication(slug);

  const app = useMemo<SampleApp>(() => fromApi(slug, apiDetail), [slug, apiDetail]);
  const { progress: initialProgress, activePhase: initialActivePhase } =
    deriveProgress(app);

  const [tab, setTab] = useState<TabName>('pipeline');
  const [activePhase, setActivePhase] = useState<number>(initialActivePhase);
  const [running, setRunning] = useState<boolean>(app.status === 'running');
  const [progress, setProgress] = useState<ProgressMap>(initialProgress);
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [runError, setRunError] = useState<string | null>(null);

  // Ref to hold the active EventSource so we can close it on cancel/unmount.
  const esRef = useRef<EventSource | null>(null);

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
        } else if (data.status === 'running') {
          setProgress(p => ({ ...p, [phaseNum]: Math.max(p[phaseNum], 10) }));
          setActivePhase(phaseNum);
          setRunning(true);
        } else if (data.status === 'failed') {
          setRunning(false);
        }
        // Add a log entry for the phase event.
        const msg = `&lt;&lt;PHASE&gt;&gt; ${data.phase} status=${data.status}`;
        setEvents(ev => [...ev, { ts: now(), lvl: 'done', msg }]);
      } catch { /* ignore malformed event */ }
    });

    es.addEventListener('specialist', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data as string) as SseSpecialistEvent;
        const specialist = redactSensitive(String(data.specialist ?? ''));
        const kindLabel = redactSensitive(String(data.kind_label ?? ''));
        const msg = `<span class="dim">specialist=</span>${specialist} <span class="dim">kind=</span>${kindLabel}`;
        setEvents(ev => ev.length > 400 ? ev : [...ev, { ts: now(), lvl: 'spec', msg }]);
        // Advance phase bar progress incrementally for each specialist.
        const phaseNum = ssePhaseToNum(data.phase);
        setProgress(p => ({
          ...p,
          [phaseNum]: Math.min(90, p[phaseNum] + 15),
        }));
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
        setEvents(ev => ev.length > 400 ? ev : [...ev, { ts: now(), lvl, msg: line }]);
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

  // ── Re-run apply handler (real API) ──────────────────────────────────
  const handleReRun = useCallback(async () => {
    setRunError(null);
    // Reset state for a fresh run.
    setProgress({ 1: 0, 2: 0, 3: 0 });
    setActivePhase(1);
    setEvents([{ ts: now(), lvl: 'info', msg: `<span class="dim">apply</span> start <span class="dim">slug=</span>${slug}` }]);
    setRunning(true);

    try {
      // POST /api/applications — the URL lives in app.url (may be empty for historical apps).
      const url = app.url || `https://placeholder/${slug}`;
      await postApplication(url, slug);
      subscribeToEvents(slug);
    } catch (err) {
      setRunning(false);
      const raw = err instanceof JobsmithApiError ? err.message : String(err);
      const msg = redactSensitive(raw);
      setRunError(msg);
      // Re-add a failed event.
      setEvents(ev => [...ev, { ts: now(), lvl: 'warn', msg: `launch failed: ${msg}` }]);
    }
  }, [slug, app.url, subscribeToEvents]);

  // ── Cancel handler ───────────────────────────────────────────────────
  const handleCancel = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    setRunning(false);
    setEvents(ev => [...ev, { ts: now(), lvl: 'warn', msg: 'run cancelled by user' }]);
  }, []);

  // Close SSE on unmount.
  useEffect(() => {
    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, []);

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
            <StatusBadge status={running ? 'running' : app.status} />
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
          <button className="btn"><Icon name="doc" size={13} /> open in marimo</button>
          <button className="btn"><Icon name="folder" size={13} /> reveal in finder</button>
          {!running && (
            <button className="btn primary" onClick={() => { void handleReRun(); }}>
              <Icon name="play" size={12} /> re-run apply
            </button>
          )}
          {running && (
            <button className="btn danger" onClick={handleCancel}>
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

      {tab === 'pipeline' && <PipelineTab events={events} running={running} phase={activePhase} progress={progress} />}
      {tab === 'artifacts' && <ArtifactsTab artifacts={apiDetail?.artifacts ?? []} />}
      {tab === 'factcheck' && <FactCheckTab artifacts={apiDetail?.artifacts ?? []} />}
      {tab === 'anchors' && <AnchorCheckTab artifacts={apiDetail?.artifacts ?? []} />}
      {tab === 'config' && <ConfigTab app={app} />}
    </div>
  );
}
