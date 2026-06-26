// views.tsx — port of design/app/views.jsx
//
// Exports: SiteView, FeedbackView, DoctorView, ConfigView

import { useState, useEffect, useCallback } from 'react';
import { Icon, Badge } from './shared';
import { apiPost, apiPut, JobsmithApiError } from '../api/client';
import { useApplications, useMasterSection, useConfig, useFeedback, useDoctor } from '../api/hooks';
import type { MasterAuthor, ApplicationRow, JobsmithConfig, LLMProvider, ConfigValidateResponse, ConfigValidationError } from '../api/types';

// ── SiteView ─────────────────────────────────────────────────────────────

type SiteMode = 'public' | 'private';

/** Derive a display-friendly "sent X ago" string from an ISO timestamp. */
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

/** Derive a human-readable full name from the author object. */
function authorFullName(author: MasterAuthor | null | undefined): string {
  if (!author) return '';
  if (author.name && typeof author.name === 'object') {
    const n = author.name as Record<string, string>;
    return [n['first'], n['middle'], n['last']].filter(Boolean).join(' ');
  }
  if (author.firstname || author.lastname) {
    return [author.firstname, author.lastname].filter(Boolean).join(' ');
  }
  return '';
}

export function SiteView() {
  const [mode, setMode] = useState<SiteMode>('public');

  const { data: author, isLoading: authorLoading } = useMasterSection('author');
  const { data: allApps = [], isLoading: appsLoading } = useApplications();

  // Filter on ui_phase (the UI taxonomy added by the API for exactly this
  // purpose). Raw status is `done` / `backfilled` for completed runs so a
  // status==='rendered' check would always be empty (roborev job 940).
  const renderedApps: ApplicationRow[] = allApps.filter(
    (a) => a.ui_phase === 'rendered',
  );

  const homepage = author?.homepage ?? '';
  const fullName = authorFullName(author);
  const urlBarLabel =
    mode === 'public'
      ? `${homepage || '…'}/applications`
      : 'localhost:4200 (private)';
  const authorLabel = fullName ? `${fullName.toLowerCase()} · applications` : 'applications';

  const isLoading = authorLoading || appsLoading;

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>listings site</h1>
          <p>a quarto-rendered index of every assembled application — share-ready, with a private mode for personal review.</p>
        </div>
        <div className="actions">
          <div style={{ display: 'flex', gap: 0, border: '1px solid var(--border-strong)', borderRadius: 'var(--radius)', overflow: 'hidden' }}>
            {(['public', 'private'] as SiteMode[]).map(m => (
              <span
                key={m}
                className="mono-sm"
                onClick={() => setMode(m)}
                style={{
                  padding: '6px 12px',
                  cursor: 'pointer',
                  background: mode === m ? 'var(--bg-sunk)' : 'var(--bg-elev)',
                  color: mode === m ? 'var(--fg)' : 'var(--fg-muted)',
                  borderRight: m === 'public' ? '1px solid var(--border)' : 'none',
                  fontSize: 12,
                }}
              >
                {m}
              </span>
            ))}
          </div>
          {/*
            "serve" + "render" buttons removed in feat-aba75dae (GH#53).
            CLI commands `jobsmith site serve` / `jobsmith site render`
            exist, but neither has a backing API endpoint, so the buttons
            had no working effect. Run those commands from the terminal
            for now; reintroduce here once /api/site/render and
            /api/site/serve land.
          */}
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 320px', gap: 16 }}>
        <div className="card" style={{ overflow: 'hidden' }}>
          <div className="card-h" style={{ background: 'var(--bg-sunk)' }}>
            <div style={{ display: 'flex', gap: 6 }}>
              <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#ff5f56' }}></span>
              <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#ffbd2e' }}></span>
              <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#27c93f' }}></span>
            </div>
            <div className="mono-sm" style={{ marginLeft: 14, color: 'var(--fg-muted)' }}>
              {urlBarLabel}
            </div>
          </div>
          <div style={{ padding: '40px 56px', background: 'var(--bg-elev)', minHeight: 520 }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', letterSpacing: '0.08em', textTransform: 'uppercase', marginBottom: 14 }}>{authorLabel}</div>
            <div style={{ fontSize: 32, fontWeight: 600, letterSpacing: '-0.025em', marginBottom: 6 }}>open applications</div>
            <div style={{ color: 'var(--fg-muted)', fontSize: 14, marginBottom: 32, maxWidth: 520 }}>
              every role i'm interested in, with the resume + cover i actually sent — generated by jobsmith and rendered through quarto.
            </div>

            {isLoading ? (
              <div style={{ color: 'var(--fg-subtle)', fontSize: 13 }}>loading…</div>
            ) : renderedApps.length === 0 ? (
              <div style={{ color: 'var(--fg-subtle)', fontSize: 13 }}>no rendered applications yet.</div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 0, borderTop: '1px solid var(--border)' }}>
                {renderedApps.map(a => (
                  <div
                    key={a.slug}
                    style={{
                      padding: '18px 0',
                      borderBottom: '1px solid var(--border)',
                      display: 'grid',
                      gridTemplateColumns: '1fr auto',
                      gap: 18,
                      alignItems: 'center',
                    }}
                  >
                    <div>
                      <div style={{ fontSize: 16, fontWeight: 500, letterSpacing: '-0.005em' }}>{a.slug}</div>
                      <div style={{ color: 'var(--fg-muted)', fontSize: 13, marginTop: 2 }}>
                        sent {relativeTime(a.finished_at ?? a.started_at)}
                      </div>
                      {mode === 'private' && (
                        <div className="mono-sm" style={{ color: 'var(--fg-subtle)', marginTop: 4 }}>{a.phase} · {a.status}</div>
                      )}
                    </div>
                    <Icon name="arrow" size={14} style={{ color: 'var(--fg-subtle)' }} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card">
            <div className="card-h"><h3>render mode</h3></div>
            <div style={{ padding: '14px 16px', fontSize: 13, color: 'var(--fg-muted)', lineHeight: 1.55 }}>
              {mode === 'public'
                ? <span><b style={{ color: 'var(--fg)' }}>public</b> strips private-tagged variables (slug, drop reasons, internal notes) before rendering.</span>
                : <span><b style={{ color: 'var(--fg)' }}>private</b> renders everything — slugs, fact-check trails, anchor coverage. for your eyes only.</span>}
            </div>
          </div>

          <div className="card">
            <div className="card-h"><h3>last render</h3><span className="sub mono-sm">2.4s</span></div>
            <div style={{ padding: '14px 16px', fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--fg-muted)', lineHeight: 1.9 }}>
              <div><Icon name="check" size={11} style={{ color: 'var(--success)', marginRight: 6 }} />quarto render</div>
              <div><Icon name="check" size={11} style={{ color: 'var(--success)', marginRight: 6 }} />{renderedApps.length} listings indexed</div>
              <div><Icon name="check" size={11} style={{ color: 'var(--success)', marginRight: 6 }} />sitemap.xml written</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── FeedbackView ─────────────────────────────────────────────────────────

/** Format an ISO timestamp as a YYYY-MM-DD date string. */
function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().slice(0, 10);
}

/** Compose a one-line summary from a feedback record. Prefers `lesson`. */
function feedbackSummary(r: { before: string; after: string; lesson: string }): string {
  if (r.lesson) return r.lesson;
  if (r.before && r.after) return `${r.before} → ${r.after}`;
  return r.after || r.before || '';
}

export function FeedbackView() {
  const { data: rows = [], isLoading, error } = useFeedback();

  const editCount = rows.filter(r => r.kind !== 'outcome').length;
  const outcomeCount = rows.filter(r => r.kind === 'outcome').length;
  const slugCount = new Set(rows.map(r => r.slug)).size;

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>feedback</h1>
          <p>captured edits and outcomes per slug. trains the bullet-selector and cover-drafter over time.</p>
        </div>
        <div className="actions">
          {/*
            "export json", "prune", and "record" buttons removed in
            feat-aba75dae (GH#53). None had handlers — no client-side
            download, no DELETE endpoint, no POST /api/feedback flow.
            Use `jobsmith feedback record …` from the CLI for now.
            Reintroduce here once corresponding API surface exists.
          */}
        </div>
      </div>

      <div className="stat-row" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
        <div className="stat">
          <div className="label">total entries</div>
          <div className="value">{rows.length}</div>
          <div className="delta">across {slugCount} slug{slugCount === 1 ? '' : 's'}</div>
        </div>
        <div className="stat">
          <div className="label">edits captured</div>
          <div className="value">{editCount}</div>
          <div className="delta"></div>
        </div>
        <div className="stat">
          <div className="label">outcomes</div>
          <div className="value">{outcomeCount}</div>
          <div className="delta"></div>
        </div>
      </div>

      <div className="card">
        <div className="card-h">
          <h3>recent entries</h3>
          <span className="sub mono-sm">private/feedback.db</span>
        </div>
        {error ? (
          <div style={{ padding: '14px 16px', color: 'var(--danger, var(--fg-muted))', fontSize: 13 }}>
            failed to load feedback: {error.message}
          </div>
        ) : isLoading ? (
          <div style={{ padding: '14px 16px', color: 'var(--fg-subtle)', fontSize: 13 }}>loading…</div>
        ) : rows.length === 0 ? (
          <div style={{ padding: '14px 16px', color: 'var(--fg-subtle)', fontSize: 13 }}>no feedback recorded yet.</div>
        ) : (
          <table className="table">
            <thead>
              <tr><th>date</th><th>slug</th><th>kind</th><th>summary</th></tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.slug}-${r.timestamp}-${i}`} className="row-clickable">
                  <td><span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>{formatDate(r.timestamp)}</span></td>
                  <td><span className="slug">{r.slug}</span></td>
                  <td>{r.kind === 'outcome' ? <Badge kind="success">outcome</Badge> : <Badge kind="accent">{r.kind || 'edit'}</Badge>}</td>
                  <td style={{ fontSize: 13 }}>{feedbackSummary(r)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ── DoctorView ───────────────────────────────────────────────────────────

function doctorBadge(status: 'pass' | 'warn' | 'fail') {
  if (status === 'pass') return <Badge kind="success">ok</Badge>;
  if (status === 'warn') return <Badge kind="warn">warn</Badge>;
  return <Badge kind="danger">fail</Badge>;
}

export function DoctorView() {
  const { data: checks = [], isLoading, error, refetch } = useDoctor();

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>doctor</h1>
          <p>preflight environment checks. the same set <span className="mono">jobsmith doctor</span> runs from the CLI.</p>
        </div>
        <div className="actions">
          <button
            className="btn primary"
            onClick={refetch}
            disabled={isLoading}
          >
            <Icon name="play" size={12} /> {isLoading ? 'running…' : 're-run checks'}
          </button>
        </div>
      </div>
      <div className="card">
        {error ? (
          <div style={{ padding: '14px 16px', color: 'var(--danger, var(--fg-muted))', fontSize: 13 }}>
            failed to load checks: {error.message}
          </div>
        ) : isLoading && checks.length === 0 ? (
          <div style={{ padding: '14px 16px', color: 'var(--fg-subtle)', fontSize: 13 }}>loading…</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: '24%' }}>check</th>
                <th>detail</th>
                <th style={{ width: 120 }}>status</th>
              </tr>
            </thead>
            <tbody>
              {checks.map(c => (
                <tr key={c.name}>
                  <td><span className="mono-sm">{c.name}</span></td>
                  <td style={{ color: 'var(--fg-muted)', fontSize: 13 }}>{c.message}</td>
                  <td>{doctorBadge(c.status)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ── ConfigView ───────────────────────────────────────────────────────────


type SaveStatus = 'idle' | 'saving' | 'saved' | 'error';
type ValidateStatus = 'idle' | 'validating' | 'valid' | 'invalid' | 'error';

/** Safely extract field errors from a 422 JobsmithApiError detail payload. */
function extract422Errors(err: unknown): ConfigValidationError[] {
  if (!(err instanceof JobsmithApiError) || err.status !== 422) return [];
  try {
    const parsed = JSON.parse(err.message);
    if (Array.isArray(parsed)) {
      return parsed as ConfigValidationError[];
    }
  } catch {
    // message wasn't JSON — fall through
  }
  return [{ field: 'root', message: err.message }];
}

export function ConfigView() {
  const { data: remoteConfig, isLoading, error: loadError } = useConfig();

  // Local controlled state — mirrors the subset of fields shown in the UI.
  // master paths
  const [workYml, setWorkYml] = useState('');
  const [skillYml, setSkillYml] = useState('');
  const [educationYml, setEducationYml] = useState('');
  const [authorYml, setAuthorYml] = useState('');
  // output paths
  const [applicationsDir, setApplicationsDir] = useState('');
  const [jobsmithDb, setJobsmithDb] = useState('');
  // llm settings
  const [llmProvider, setLlmProvider] = useState<LLMProvider>('claude_cli');
  const [llmBaseUrl, setLlmBaseUrl] = useState('');
  const [llmModel, setLlmModel] = useState('');

  // Feedback state
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [validateStatus, setValidateStatus] = useState<ValidateStatus>('idle');
  const [fieldErrors, setFieldErrors] = useState<ConfigValidationError[]>([]);
  const [saveError, setSaveError] = useState<string>('');

  // Hydrate controlled inputs when GET resolves.
  useEffect(() => {
    if (!remoteConfig) return;
    setWorkYml(String(remoteConfig.master?.work_yml ?? ''));
    setSkillYml(String(remoteConfig.master?.skill_yml ?? ''));
    setEducationYml(String(remoteConfig.master?.education_yml ?? ''));
    setAuthorYml(String(remoteConfig.master?.author_yml ?? ''));
    setApplicationsDir(String(remoteConfig.output?.applications_dir ?? ''));
    setJobsmithDb(String(remoteConfig.output?.jobsmith_db ?? ''));
    setLlmProvider((remoteConfig.llm?.provider ?? 'claude_cli') as LLMProvider);
    setLlmBaseUrl(remoteConfig.llm?.base_url ?? '');
    setLlmModel(remoteConfig.llm?.model ?? '');
  }, [remoteConfig]);

  /** Build a config payload from current UI state, merged over remote defaults. */
  const buildPayload = useCallback((): Record<string, unknown> => {
    return {
      ...(remoteConfig ?? {}),
      master: {
        ...(remoteConfig?.master ?? {}),
        work_yml: workYml,
        skill_yml: skillYml,
        education_yml: educationYml,
        author_yml: authorYml,
      },
      output: {
        ...(remoteConfig?.output ?? {}),
        applications_dir: applicationsDir,
        jobsmith_db: jobsmithDb,
      },
      llm: {
        ...(remoteConfig?.llm ?? {}),
        provider: llmProvider,
        base_url: llmBaseUrl || null,
        model: llmModel || null,
      },
    };
  }, [remoteConfig, workYml, skillYml, educationYml, authorYml, applicationsDir, jobsmithDb, llmProvider, llmBaseUrl, llmModel]);

  const handleValidate = useCallback(async () => {
    setValidateStatus('validating');
    setFieldErrors([]);
    try {
      const result = await apiPost<ConfigValidateResponse>('/api/config/validate', buildPayload() as unknown);
      if (result.ok) {
        setValidateStatus('valid');
      } else {
        setFieldErrors(result.errors);
        setValidateStatus('invalid');
      }
    } catch (err) {
      setValidateStatus('error');
      setSaveError(err instanceof Error ? err.message : String(err));
    }
  }, [buildPayload]);

  const handleSave = useCallback(async () => {
    setSaveStatus('saving');
    setFieldErrors([]);
    setSaveError('');
    try {
      await apiPut<JobsmithConfig>('/api/config', buildPayload() as unknown);
      setSaveStatus('saved');
    } catch (err) {
      const errors422 = extract422Errors(err);
      if (errors422.length > 0) {
        setFieldErrors(errors422);
        setSaveError('422: validation errors — see details above.');
      } else {
        setSaveError(err instanceof Error ? err.message : String(err));
      }
      setSaveStatus('error');
    }
  }, [buildPayload]);

  if (isLoading) {
    return (
      <div className="content">
        <div className="page-head"><div><h1>config</h1></div></div>
        <div style={{ padding: 32, color: 'var(--fg-muted)', fontSize: 13 }}>loading config…</div>
      </div>
    );
  }

  // GET /api/config failed. Don't render the form — saving from blank local
  // state would PUT a partial config and clobber the remote file.
  if (loadError) {
    return (
      <div className="content">
        <div className="page-head"><div><h1>config</h1></div></div>
        <div
          style={{ padding: 24, color: 'var(--danger, var(--fg-muted))', fontSize: 13 }}
          role="alert"
        >
          failed to load config: {loadError.message}
        </div>
      </div>
    );
  }

  const hasErrors = fieldErrors.length > 0;
  const formDisabled = !remoteConfig;

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>config</h1>
          <p>workspace settings — written to <span className="mono">.apply-config.yaml</span>.</p>
        </div>
        <div className="actions">
          {validateStatus === 'valid' && (
            <span style={{ fontSize: 12, color: 'var(--success)', marginRight: 4 }}>config is valid</span>
          )}
          {validateStatus === 'invalid' && (
            <span style={{ fontSize: 12, color: 'var(--error, #e55)', marginRight: 4 }}>invalid — see errors below</span>
          )}
          {validateStatus === 'error' && (
            <span style={{ fontSize: 12, color: 'var(--error, #e55)', marginRight: 4 }}>validate failed — see details below</span>
          )}
          {saveStatus === 'saved' && (
            <span style={{ fontSize: 12, color: 'var(--success)', marginRight: 4 }}>saved</span>
          )}
          {saveStatus === 'error' && (
            <span style={{ fontSize: 12, color: 'var(--error, #e55)', marginRight: 4 }}>{saveError || 'save failed'}</span>
          )}
          <button
            type="button"
            className="btn"
            disabled={formDisabled || validateStatus === 'validating'}
            onClick={handleValidate}
          >
            <Icon name="doc" size={13} /> validate
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={formDisabled || saveStatus === 'saving'}
            onClick={handleSave}
          >
            <Icon name="check" size={12} /> save
          </button>
        </div>
      </div>

      {validateStatus === 'error' && saveError && (
        <div
          style={{ marginBottom: 16, padding: '12px 16px', background: 'var(--bg-elev)', border: '1px solid var(--error, #e55)', borderRadius: 'var(--radius)', fontSize: 13, color: 'var(--fg-muted)' }}
          role="alert"
        >
          <div style={{ fontWeight: 600, marginBottom: 4, color: 'var(--error, #e55)' }}>validate request failed</div>
          {saveError}
        </div>
      )}

      {hasErrors && (
        <div style={{ marginBottom: 16, padding: '12px 16px', background: 'var(--bg-elev)', border: '1px solid var(--error, #e55)', borderRadius: 'var(--radius)', fontSize: 13 }}>
          <div style={{ fontWeight: 600, marginBottom: 6, color: 'var(--error, #e55)' }}>validation errors</div>
          {fieldErrors.map((e, i) => (
            <div key={i} style={{ color: 'var(--fg-muted)', fontFamily: 'var(--font-mono)', fontSize: 12, marginTop: 2 }}>
              <span style={{ color: 'var(--fg)' }}>{e.field}</span> — {e.message}
            </div>
          ))}
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div className="card">
          <div className="card-h"><h3>workspace</h3></div>
          <div style={{ padding: '16px 18px' }}>
            <div className="field">
              <label>applications dir</label>
              <input className="mono" value={applicationsDir} onChange={e => setApplicationsDir(e.target.value)} />
            </div>
            <div className="field">
              <label>jobsmith db</label>
              <input className="mono" value={jobsmithDb} onChange={e => setJobsmithDb(e.target.value)} />
            </div>
          </div>
        </div>
        <div className="card">
          <div className="card-h"><h3>master files</h3></div>
          <div style={{ padding: '16px 18px' }}>
            <div className="field">
              <label>work</label>
              <input className="mono" value={workYml} onChange={e => setWorkYml(e.target.value)} />
            </div>
            <div className="field">
              <label>skills</label>
              <input className="mono" value={skillYml} onChange={e => setSkillYml(e.target.value)} />
            </div>
            <div className="field">
              <label>education</label>
              <input className="mono" value={educationYml} onChange={e => setEducationYml(e.target.value)} />
            </div>
            <div className="field">
              <label>author</label>
              <input className="mono" value={authorYml} onChange={e => setAuthorYml(e.target.value)} />
            </div>
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-h"><h3>llm provider</h3></div>
        <div style={{ padding: '16px 18px' }}>
          <div className="field">
            <label htmlFor="llm-provider-select">provider</label>
            <select
              id="llm-provider-select"
              value={llmProvider}
              onChange={e => setLlmProvider(e.target.value as LLMProvider)}
            >
              <option value="claude_cli">Claude CLI (default)</option>
              <option value="antigravity_cli">Antigravity</option>
              <option value="codex_cli">Codex</option>
              <option value="openai_compatible">Local (OpenAI-compatible)</option>
            </select>
          </div>
          <div className="field">
            <label>presets</label>
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                className="btn"
                onClick={() => { setLlmProvider('openai_compatible'); setLlmBaseUrl('http://127.0.0.1:8080/v1'); }}
              >
                MLX
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => { setLlmProvider('openai_compatible'); setLlmBaseUrl('http://localhost:11434/v1'); }}
              >
                Ollama
              </button>
            </div>
          </div>
          {llmProvider === 'openai_compatible' && (
            <>
              <div className="field">
                <label htmlFor="llm-base-url-input">base url</label>
                <input
                  id="llm-base-url-input"
                  className="mono"
                  value={llmBaseUrl}
                  onChange={e => setLlmBaseUrl(e.target.value)}
                />
              </div>
              <div className="field">
                <label htmlFor="llm-model-input">model</label>
                <input
                  id="llm-model-input"
                  className="mono"
                  value={llmModel}
                  onChange={e => setLlmModel(e.target.value)}
                />
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
