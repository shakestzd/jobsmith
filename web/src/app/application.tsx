// application.tsx — port of design/app/application.jsx
//
// Exports: ApplicationDetail (top-level).
// Sub-components (PhaseCard, PipelineTab, ArtifactsTab, PdfPreview,
// FactCheckTab, AnchorCheckTab, ConfigTab) are file-local.
//
// DOM structure, class names, and visual behaviour are pixel-identical to
// the design source.

import { useState, useEffect, useRef } from 'react';
import type { SampleApp, IconName } from '../types';
import { Icon, Badge, StatusBadge, SAMPLE_APPS } from './shared';
import { useRerunApplication } from '../api/hooks';
import { ApiError } from '../api/client';

// ── Public prop type ─────────────────────────────────────────────────────────

export interface ApplicationDetailProps {
  /** The application slug to display. Falls back to SAMPLE_APPS[0] if not found. */
  slug: string;
  /** Navigate back to the applications list. */
  back: () => void;
}

// ── File-tree node shape ─────────────────────────────────────────────────────

interface TreeFile {
  kind: 'file';
  name: string;
  size: string;
  highlight?: boolean;
}

interface TreeDir {
  kind: 'dir';
  name: string;
  open: boolean;
  children: TreeFile[];
}

type TreeNode = TreeDir | TreeFile;

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

const NEW_EVENTS: Omit<LogEvent, 'ts'>[] = [
  { lvl: 'info', msg: '<span class="dim">phase=</span>render <span class="dim">spec=</span>apply-renderer' },
  { lvl: 'tool', msg: 'Bash: <span class="dim">quarto render index.qmd</span>' },
  { lvl: 'tool', msg: 'Read: <span class="dim">.apply-state/cover_draft.md</span>' },
  { lvl: 'spec', msg: 'apply-assembler: writing _variables.yml' },
  { lvl: 'info', msg: 'sub-agent <span class="dim">apply-renderer</span> handed off to quarto' },
  { lvl: 'tool', msg: 'Write: <span class="dim">rendered/resume.pdf</span> (92 KB)' },
];

function seedEvents(app: SampleApp): LogEvent[] {
  return [
    { ts: '14:02:01', lvl: 'info', msg: '<span class="dim">apply</span> start <span class="dim">slug=</span>' + app.slug },
    { ts: '14:02:01', lvl: 'info', msg: '<span class="dim">phase=</span>gather' },
    { ts: '14:02:02', lvl: 'tool', msg: 'WebFetch: ' + app.url },
    { ts: '14:02:04', lvl: 'spec', msg: 'apply-jd-parser: extracted 18 requirements, 5 must-haves' },
    { ts: '14:02:05', lvl: 'spec', msg: 'apply-anchor-scorer: 14 anchors, top match deploy-pipeline-rebuild (0.92)' },
    { ts: '14:02:06', lvl: 'tool', msg: 'Write: <span class="dim">.apply-state/spec.json</span>' },
    { ts: '14:02:06', lvl: 'done', msg: '&lt;&lt;PHASE_COMPLETE&gt;&gt; gather (1.4s)' },
    { ts: '14:02:07', lvl: 'info', msg: '<span class="dim">phase=</span>draft' },
    { ts: '14:02:09', lvl: 'spec', msg: 'apply-bullet-selector: selected 14 bullets, dropped 0' },
    { ts: '14:02:10', lvl: 'spec', msg: 'apply-cover-drafter: 312 words, 4 paragraphs' },
    { ts: '14:02:11', lvl: 'spec', msg: 'apply-factchecker: 5/5 claims verified' },
    { ts: '14:02:11', lvl: 'tool', msg: 'Write: <span class="dim">.apply-state/cover_draft.md</span>' },
    { ts: '14:02:11', lvl: 'done', msg: '&lt;&lt;PHASE_COMPLETE&gt;&gt; draft (3.8s)' },
    { ts: '14:02:12', lvl: 'info', msg: '<span class="dim">phase=</span>render' },
    { ts: '14:02:14', lvl: 'spec', msg: 'apply-assembler: wrote _variables.yml (12 vars)' },
  ];
}

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
  app: SampleApp;
}

function ArtifactsTab({ app }: ArtifactsTabProps) {
  const [sel, setSel] = useState<string>('cover_draft.md');

  const tree: TreeNode[] = [
    {
      kind: 'dir', name: '.apply-state/', open: true, children: [
        { kind: 'file', name: 'spec.json', size: '2.1 KB' },
        { kind: 'file', name: 'bullet_selection.json', size: '4.8 KB' },
        { kind: 'file', name: 'cover_draft.md', size: '1.6 KB', highlight: true },
        { kind: 'file', name: 'fact_check.json', size: '820 B' },
        { kind: 'file', name: 'anchor_check.json', size: '440 B' },
      ],
    },
    {
      kind: 'dir', name: 'rendered/', open: true, children: [
        { kind: 'file', name: 'resume.pdf', size: '92 KB' },
        { kind: 'file', name: 'cover.pdf', size: '58 KB' },
        { kind: 'file', name: 'index.qmd', size: '3.2 KB' },
        { kind: 'file', name: '_variables.yml', size: '1.1 KB' },
      ],
    },
  ];

  const PREVIEWS: Record<string, string> = {
    'spec.json': `{
  "slug": "${app.slug}",
  "company": "${app.company}",
  "role": "${app.role}",
  "url": "${app.url}",
  "must_haves": [
    "deep typescript + node",
    "shipped customer-facing CLI",
    "performance work"
  ],
  "nice_to_haves": ["rust", "edge runtime"],
  "themes": ["developer experience", "platform"]
}`,
    'cover_draft.md': `# Cover — ${app.role}, ${app.company}

I've been a heavy ${app.company} user for the past three years — the kind
of user who reads the changelog. The bits of your stack that have stayed
with me are exactly where I want to spend the next chapter of work.

In my last role at Recurly Engineering, I rebuilt the deploy pipeline
(11m → 2m20s median) and shipped the artifact-cache layer that now
serves 1.2B requests/month at p99 < 38ms. The work that mattered most
wasn't either of those wins on its own — it was the cultural turn from
"prod is scary" to "prod is boring."

That's the work I want to bring to ${app.company}. ...`,
    'fact_check.json': `{
  "claims": [
    { "claim": "11m → 2m20s deploy time", "source": "work.yml#deploy-pipeline", "ok": true },
    { "claim": "1.2B requests/month",     "source": "work.yml#artifact-cache",  "ok": true },
    { "claim": "p99 < 38ms",              "source": "work.yml#artifact-cache",  "ok": true },
    { "claim": "$140k/yr recovered",      "source": "work.yml#scheduler-mig",   "ok": true },
    { "claim": "180 engineers",           "source": "work.yml#dev-env",         "ok": true }
  ],
  "unverified": [],
  "summary": "5 / 5 claims verified against master YAML"
}`,
    'bullet_selection.json': `{
  "selected": [
    "deploy-pipeline-rebuild",
    "artifact-cache-rust",
    "scheduler-migration",
    "live-reload-dev-env"
  ],
  "anchors_preserved": "14/14",
  "drop_reasons": {},
  "rationale": "Selected bullets emphasize platform / DX impact + measured performance work, matching the role's stated focus."
}`,
    'anchor_check.json': `{
  "total_anchors": 14,
  "preserved": 14,
  "dropped": 0,
  "ok": true
}`,
    '_variables.yml': `slug: ${app.slug}
company: "${app.company}"
role:    "${app.role}"
date:    2026-04-30
selected_bullets:
  - id: deploy-pipeline-rebuild
  - id: artifact-cache-rust
  - id: scheduler-migration
  - id: live-reload-dev-env
cover_path: cover_draft.md`,
    'index.qmd': `---
title: "{{< var role >}} — {{< var company >}}"
format:
  jobsmith-resume-pdf: default
  jobsmith-cover-pdf:
    output-file: cover.pdf
---
{{< include _bullets.qmd >}}
{{< include _cover.qmd >}}`,
    'resume.pdf': '__PDF_PREVIEW__',
    'cover.pdf': '__PDF_PREVIEW__',
  };

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
      <div className="card" style={{ padding: '12px' }}>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', padding: '4px 8px 8px' }}>artifacts</div>
        <div className="tree">
          {tree.map((node, i) => {
            if (node.kind !== 'dir') return null;
            const dir = node as TreeDir;
            return (
              <div key={i}>
                <div className="tree-row">
                  <Icon name="chevd" size={10} className="caret" />
                  <Icon name="folder" size={12} className="ico" />
                  <span style={{ color: 'var(--fg)' }}>{dir.name}</span>
                </div>
                <div className="tree-children">
                  {dir.children.map(f => (
                    <div
                      key={f.name}
                      className={`tree-row ${sel === f.name ? 'active' : ''}`}
                      onClick={() => setSel(f.name)}
                    >
                      <span className="caret" />
                      <Icon name="doc" size={11} className="ico" />
                      <span style={{ flex: 1 }}>{f.name}</span>
                      <span style={{ color: 'var(--fg-subtle)', fontSize: 10.5 }}>{f.size}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="card">
        <div className="card-h">
          <Icon name="doc" size={13} />
          <h3 style={{ fontFamily: 'var(--font-mono)', fontWeight: 500 }}>{sel}</h3>
          <div className="right">
            <button className="btn ghost sm">copy</button>
            <button className="btn ghost sm">open</button>
          </div>
        </div>
        {PREVIEWS[sel] === '__PDF_PREVIEW__' ? (
          <PdfPreview name={sel} />
        ) : (
          <pre className="code" style={{ border: 'none', borderRadius: 0, margin: 0, maxHeight: 560 }}>{PREVIEWS[sel]}</pre>
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

interface FactClaim {
  c: string;
  src: string;
  ok: boolean;
}

function FactCheckTab() {
  const claims: FactClaim[] = [
    { c: '11m → 2m20s deploy time', src: 'work.yml#deploy-pipeline', ok: true },
    { c: '1.2B requests/month', src: 'work.yml#artifact-cache', ok: true },
    { c: 'p99 < 38ms', src: 'work.yml#artifact-cache', ok: true },
    { c: '$140k/yr recovered', src: 'work.yml#scheduler-mig', ok: true },
    { c: '180 engineers', src: 'work.yml#dev-env', ok: true },
    { c: '320 services migrated', src: 'work.yml#scheduler-mig', ok: true },
    { c: '42% page volume reduction', src: 'work.yml#oncall', ok: true },
  ];
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

function AnchorCheckTab() {
  const anchors = [
    'deploy-pipeline-rebuild',
    'artifact-cache-rust',
    'scheduler-migration',
    'live-reload-dev-env',
    'oss-rust-style-guide',
    'team-onboarding-mentorship',
    'dev-env-cold-start',
    'rust-rfc-3-accepted',
    'platform-cost-recovery',
    'jd-parser-shipped',
    'q3-incident-response',
    'quarto-tooling-talk',
    'onboarding-handbook-rewrite',
    'cli-launch-200-stars',
  ];
  return (
    <div className="card">
      <div className="card-h">
        <h3>anchor preservation</h3>
        <span className="sub">bullet_selection.json</span>
        <div className="right"><Badge kind="success">14 / 14 preserved</Badge></div>
      </div>
      <div style={{ padding: '18px 20px', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24 }}>
        <div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>preserved anchors</div>
          {anchors.map(a => (
            <div key={a} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '5px 0' }}>
              <Icon name="check" size={11} style={{ color: 'var(--success)' }} />
              <span className="mono-sm">{a}</span>
            </div>
          ))}
        </div>
        <div>
          <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>dropped (with reasons)</div>
          <div style={{ padding: '24px', textAlign: 'center', color: 'var(--fg-subtle)', background: 'var(--bg-sunk)', borderRadius: 'var(--radius)', border: '1px dashed var(--border)' }}>
            <div className="mono-sm">none</div>
            <div style={{ fontSize: 12, marginTop: 4 }}>every anchor made it into this draft.</div>
          </div>
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
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      <div className="card">
        <div className="card-h"><h3>.apply-config.yaml</h3></div>
        <pre className="code" style={{ border: 'none', borderRadius: 0, margin: 0 }}>{`author: jordan-smith
master:
  work:      master/work.yml
  skills:    master/skill.yml
  education: master/education.yml
benchmark:   master/benchmark.md
output:
  resume:    rendered/resume.pdf
  cover:     rendered/cover.pdf
phase_timeout_s: 600`}</pre>
      </div>
      <div className="card">
        <div className="card-h"><h3>run options</h3></div>
        <div style={{ padding: '16px 18px' }}>
          <div className="field"><label>job url</label><input className="mono" defaultValue={app.url} /></div>
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
  const app = SAMPLE_APPS.find(a => a.slug === slug) ?? SAMPLE_APPS[0];
  const { progress: initialProgress, activePhase: initialActivePhase } =
    deriveProgress(app);

  const [tab, setTab] = useState<TabName>('pipeline');
  const [activePhase, setActivePhase] = useState<number>(initialActivePhase);
  const [running, setRunning] = useState<boolean>(app.status === 'running');
  const [progress, setProgress] = useState<ProgressMap>(initialProgress);
  const [events, setEvents] = useState<LogEvent[]>(() => seedEvents(app));
  const [rerunError, setRerunError] = useState<string | null>(null);
  const rerun = useRerunApplication();

  const handleRerun = async () => {
    setRerunError(null);
    try {
      await rerun.mutateAsync({ slug });
      setRunning(true);
      setTab('pipeline');
    } catch (err) {
      if (err instanceof ApiError) {
        try {
          const parsed = JSON.parse(err.body);
          setRerunError(typeof parsed.detail === 'string' ? parsed.detail : err.message);
        } catch {
          setRerunError(err.body || err.message);
        }
      } else {
        setRerunError(err instanceof Error ? err.message : String(err));
      }
    }
  };

  // Live progress sim when running
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
      setEvents(ev => {
        if (ev.length > 200) return ev;
        const pick = NEW_EVENTS[Math.floor(Math.random() * NEW_EVENTS.length)];
        return [...ev, { ...pick, ts: now() }];
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
            <button
              className="btn primary"
              onClick={handleRerun}
              disabled={rerun.isPending}
            >
              {rerun.isPending ? (
                <>queuing…</>
              ) : (
                <>
                  <Icon name="play" size={12} /> re-run apply
                </>
              )}
            </button>
          )}
          {running && (
            <button className="btn danger" onClick={() => setRunning(false)}>
              <Icon name="x" size={12} /> cancel run
            </button>
          )}
        </div>
      </div>

      {rerunError && (
        <div
          role="alert"
          style={{
            margin: '0 0 12px',
            padding: '10px 12px',
            borderRadius: 'var(--radius)',
            border: '1px solid var(--danger, #c33)',
            background: 'var(--bg-sunk)',
            color: 'var(--danger, #c33)',
            fontSize: 12.5,
          }}
        >
          {rerunError}
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

      {tab === 'pipeline' && <PipelineTab events={events} running={running} phase={activePhase} progress={progress} />}
      {tab === 'artifacts' && <ArtifactsTab app={app} />}
      {tab === 'factcheck' && <FactCheckTab />}
      {tab === 'anchors' && <AnchorCheckTab />}
      {tab === 'config' && <ConfigTab app={app} />}
    </div>
  );
}
