// onboard.tsx — Web onboarding wizard (feat-c6ee77d4)
//
// First-run flow: upload resume / LinkedIn export, paste text, or give a URL
// → POST /api/onboard → stream progress over the existing SSE channel
// → render gap-interview questions → "masters ready" state.
//
// Exports:
//   OnboardWizard  — full-page wizard (shown when no master content exists)

import { useState, useRef, useCallback, useEffect } from 'react';
import { Icon } from './shared';
import { BASE_URL, authHeaders, buildEventsUrl, JobsmithApiError } from '../api/client';

// ── Types ────────────────────────────────────────────────────────────────

type OnboardStep =
  | 'upload'       // Step 1: provide inputs
  | 'running'      // Step 2: pipeline running, streaming events
  | 'gap'          // Step 3: gap-interview questions
  | 'done'         // Step 4: masters ready
  | 'error';       // Terminal error

type InputMode = 'file' | 'paste' | 'url';

interface GapQuestion {
  section: string;
  field: string;
  prompt: string;
  required: boolean;
  hint: string;
}

interface LogLine {
  ts: number;
  msg: string;
  kind: 'info' | 'phase' | 'gap' | 'error';
}

// ── API helpers ──────────────────────────────────────────────────────────

/** Build the SSE URL for the "onboard" slug.
 *
 * Delegates to the shared ``buildEventsUrl`` so token resolution matches the
 * rest of the app — including the static Vite token path
 * (``getAccessToken() || STATIC_TOKEN``) used in static-token deployments.
 * The SSE endpoint uses slug="onboard"; events are filtered by run_id in the
 * handler.
 */
function buildOnboardEventsUrl(_runId: string): string {
  return buildEventsUrl('onboard');
}

/** POST /api/onboard with multipart form data. */
async function postOnboard(formData: FormData): Promise<{ run_id: string; status: string }> {
  // Reuse the shared auth header (getAccessToken() || STATIC_TOKEN) so
  // static-token deployments authenticate, but DROP Content-Type so the
  // browser sets the multipart boundary automatically.
  const headers = authHeaders();
  delete headers['Content-Type'];

  const res = await fetch(`${BASE_URL}/api/onboard`, {
    method: 'POST',
    headers,
    body: formData,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json() as Record<string, unknown>;
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch { /* ignore */ }
    throw new JobsmithApiError(detail, res.status);
  }
  return res.json() as Promise<{ run_id: string; status: string }>;
}

/** POST /api/onboard/{run_id}/answers */
async function postOnboardAnswers(
  runId: string,
  answers: Record<string, string>,
): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/onboard/${encodeURIComponent(runId)}/answers`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ answers }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json() as Record<string, unknown>;
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch { /* ignore */ }
    throw new JobsmithApiError(detail, res.status);
  }
}

// ── Sub-components ───────────────────────────────────────────────────────

interface UploadStepProps {
  onLaunch: (formData: FormData) => void;
}

function UploadStep({ onLaunch }: UploadStepProps) {
  const [mode, setMode] = useState<InputMode>('file');
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [linkedinFile, setLinkedinFile] = useState<File | null>(null);
  const [pasteText, setPasteText] = useState('');
  const [linkedinUrl, setLinkedinUrl] = useState('');
  const [error, setError] = useState<string | null>(null);

  const MAX_BYTES = 10 * 1024 * 1024;

  function handleResumeChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    if (f && f.size > MAX_BYTES) {
      setError(`File too large (max 10 MB). "${f.name}" is ${(f.size / 1024 / 1024).toFixed(1)} MB.`);
      return;
    }
    setError(null);
    setResumeFile(f);
  }

  function handleLinkedinChange(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0] ?? null;
    if (f && f.size > MAX_BYTES) {
      setError(`File too large (max 10 MB). "${f.name}" is ${(f.size / 1024 / 1024).toFixed(1)} MB.`);
      return;
    }
    setError(null);
    setLinkedinFile(f);
  }

  function handleStart() {
    setError(null);
    const fd = new FormData();
    if (mode === 'file') {
      if (!resumeFile && !linkedinFile) {
        setError('Please select at least one file to upload.');
        return;
      }
      if (resumeFile) fd.append('resume_file', resumeFile);
      if (linkedinFile) fd.append('linkedin_export', linkedinFile);
    } else if (mode === 'paste') {
      if (!pasteText.trim()) {
        setError('Please paste some text before continuing.');
        return;
      }
      fd.append('paste', pasteText.trim());
    } else if (mode === 'url') {
      if (!linkedinUrl.trim()) {
        setError('Please enter a LinkedIn URL.');
        return;
      }
      fd.append('linkedin_url', linkedinUrl.trim());
    }
    onLaunch(fd);
  }

  const modeOptions: [InputMode, string][] = [
    ['file', 'upload file'],
    ['paste', 'paste text'],
    ['url', 'linkedin url'],
  ];

  return (
    <div>
      <div style={{ marginBottom: 20 }}>
        <p style={{ color: 'var(--fg-muted)', fontSize: 14, lineHeight: 1.6, margin: 0 }}>
          jobsmith will parse your profile, ask a few gap questions, and build your master content files — the source of truth for every resume and cover letter you generate.
        </p>
      </div>

      <div className="field">
        <label>input source</label>
        <div style={{ display: 'flex', gap: 6 }}>
          {modeOptions.map(([id, label]) => (
            <span
              key={id}
              className={`pill ${mode === id ? 'active' : ''}`}
              onClick={() => { setMode(id); setError(null); }}
              style={{ cursor: 'pointer' }}
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      {mode === 'file' && (
        <>
          <div className="field">
            <label>resume <span style={{ color: 'var(--fg-subtle)', fontWeight: 400 }}>(PDF, DOCX, TXT, MD)</span></label>
            <input
              type="file"
              accept=".pdf,.docx,.txt,.md"
              onChange={handleResumeChange}
              style={{ fontSize: 13 }}
            />
            {resumeFile && (
              <div className="help" style={{ color: 'var(--success)' }}>
                <Icon name="check" size={11} /> {resumeFile.name} ({(resumeFile.size / 1024).toFixed(0)} KB)
              </div>
            )}
          </div>
          <div className="field">
            <label>
              linkedin export <span style={{ color: 'var(--fg-subtle)', fontWeight: 400 }}>(optional ZIP)</span>
            </label>
            <input
              type="file"
              accept=".zip"
              onChange={handleLinkedinChange}
              style={{ fontSize: 13 }}
            />
            {linkedinFile && (
              <div className="help" style={{ color: 'var(--success)' }}>
                <Icon name="check" size={11} /> {linkedinFile.name} ({(linkedinFile.size / 1024).toFixed(0)} KB)
              </div>
            )}
          </div>
        </>
      )}

      {mode === 'paste' && (
        <div className="field">
          <label>resume / profile text</label>
          <textarea
            className="mono"
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
            placeholder="Paste your resume or profile content here…"
            style={{ minHeight: 160 }}
          />
          <div className="help">plain text, markdown, or copied-and-pasted resume content.</div>
        </div>
      )}

      {mode === 'url' && (
        <div className="field">
          <label>linkedin profile url</label>
          <input
            className="mono"
            value={linkedinUrl}
            onChange={(e) => setLinkedinUrl(e.target.value)}
            placeholder="https://linkedin.com/in/yourprofile"
          />
          <div className="help">public profile — jobsmith will scrape it during onboarding.</div>
        </div>
      )}

      {error && (
        <div
          style={{ padding: '10px 12px', background: 'var(--bg-elev)', border: '1px solid var(--error, #e55)', borderRadius: 'var(--radius)', fontSize: 13, color: 'var(--error, #e55)', marginBottom: 8 }}
          role="alert"
        >
          {error}
        </div>
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20 }}>
        <button
          type="button"
          className="btn primary"
          onClick={handleStart}
        >
          <Icon name="play" size={12} /> start onboarding
        </button>
      </div>
    </div>
  );
}

// ── Running step ─────────────────────────────────────────────────────────

interface RunningStepProps {
  runId: string;
  log: LogLine[];
}

function RunningStep({ runId, log }: RunningStepProps) {
  const logEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [log]);

  return (
    <div>
      <div style={{ marginBottom: 12, color: 'var(--fg-muted)', fontSize: 13 }}>
        onboarding pipeline running… <span className="mono-sm" style={{ color: 'var(--fg-subtle)' }}>run_id={runId.slice(0, 8)}…</span>
      </div>
      <div
        style={{
          background: 'var(--bg-sunk)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius)',
          padding: '10px 14px',
          fontFamily: 'var(--font-mono)',
          fontSize: 12,
          lineHeight: 1.7,
          minHeight: 160,
          maxHeight: 320,
          overflowY: 'auto',
          color: 'var(--fg-muted)',
        }}
        role="log"
        aria-label="pipeline log"
        aria-live="polite"
      >
        {log.length === 0 ? (
          <span style={{ color: 'var(--fg-subtle)' }}>connecting…</span>
        ) : (
          log.map((l, i) => (
            <div
              key={i}
              style={{
                color: l.kind === 'error' ? 'var(--error, #e55)' :
                       l.kind === 'phase' ? 'var(--success)' :
                       l.kind === 'gap' ? 'var(--accent-soft-fg, var(--fg))' :
                       'var(--fg-muted)',
              }}
            >
              {l.msg}
            </div>
          ))
        )}
        <div ref={logEndRef} />
      </div>
      <div style={{ marginTop: 10, color: 'var(--fg-subtle)', fontSize: 12 }}>
        streaming live events — gap questions will appear automatically when ingestion completes.
      </div>
    </div>
  );
}

// ── Gap questions step ───────────────────────────────────────────────────

interface GapStepProps {
  questions: GapQuestion[];
  onSubmit: (answers: Record<string, string>) => Promise<void>;
}

function GapStep({ questions, onSubmit }: GapStepProps) {
  const [answers, setAnswers] = useState<Record<string, string>>(() =>
    Object.fromEntries(questions.map((q) => [`${q.section}.${q.field}`, ''])),
  );
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  async function handleSubmit() {
    setSubmitError(null);
    setSubmitting(true);
    try {
      await onSubmit(answers);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  }

  const requiredUnanswered = questions
    .filter((q) => q.required && !answers[`${q.section}.${q.field}`]?.trim())
    .length;

  return (
    <div>
      <div style={{ marginBottom: 16, color: 'var(--fg-muted)', fontSize: 13, lineHeight: 1.6 }}>
        jobsmith found a few gaps in your profile. please fill in what you can — optional fields can be left blank.
      </div>

      {questions.length === 0 ? (
        <div style={{ padding: '12px 16px', color: 'var(--fg-subtle)', fontSize: 13 }}>
          no gaps found — all required fields are present.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {questions.map((q) => {
            const key = `${q.section}.${q.field}`;
            return (
              <div key={key} className="field">
                <label>
                  {q.prompt}
                  {q.required && <span style={{ color: 'var(--error, #e55)', marginLeft: 4 }}>*</span>}
                  <span style={{ color: 'var(--fg-subtle)', fontFamily: 'var(--font-mono)', fontSize: 11, marginLeft: 6 }}>
                    {q.section}.{q.field}
                  </span>
                </label>
                {q.field === 'entries' ? (
                  <textarea
                    className="mono"
                    value={answers[key] ?? ''}
                    onChange={(e) => setAnswers((a) => ({ ...a, [key]: e.target.value }))}
                    placeholder={q.hint}
                    style={{ minHeight: 80 }}
                  />
                ) : (
                  <input
                    className="mono"
                    value={answers[key] ?? ''}
                    onChange={(e) => setAnswers((a) => ({ ...a, [key]: e.target.value }))}
                    placeholder={q.hint}
                  />
                )}
                {q.hint && <div className="help">{q.hint}</div>}
              </div>
            );
          })}
        </div>
      )}

      {submitError && (
        <div
          style={{ marginTop: 12, padding: '10px 12px', background: 'var(--bg-elev)', border: '1px solid var(--error, #e55)', borderRadius: 'var(--radius)', fontSize: 13, color: 'var(--error, #e55)' }}
          role="alert"
        >
          {submitError}
        </div>
      )}

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 20, gap: 8 }}>
        {requiredUnanswered > 0 && (
          <span style={{ fontSize: 12, color: 'var(--fg-subtle)', alignSelf: 'center' }}>
            {requiredUnanswered} required field{requiredUnanswered !== 1 ? 's' : ''} remaining
          </span>
        )}
        <button
          type="button"
          className="btn primary"
          onClick={handleSubmit}
          disabled={submitting}
        >
          {submitting ? 'submitting…' : <><Icon name="check" size={12} /> submit answers</>}
        </button>
      </div>
    </div>
  );
}

// ── Done step ────────────────────────────────────────────────────────────

interface DoneStepProps {
  onFinish: () => void;
}

function DoneStep({ onFinish }: DoneStepProps) {
  return (
    <div style={{ textAlign: 'center', padding: '24px 0' }}>
      <div style={{ fontSize: 36, marginBottom: 12 }}>
        <Icon name="check" size={36} style={{ color: 'var(--success)' }} />
      </div>
      <h3 style={{ margin: '0 0 8px' }}>masters ready</h3>
      <p style={{ color: 'var(--fg-muted)', fontSize: 14, lineHeight: 1.6, maxWidth: 400, margin: '0 auto 24px' }}>
        your master content files have been built. jobsmith is now ready to generate tailored resumes and cover letters.
      </p>
      <button
        type="button"
        className="btn primary"
        onClick={onFinish}
      >
        <Icon name="arrow" size={12} /> go to master content
      </button>
    </div>
  );
}

// ── Main wizard component ────────────────────────────────────────────────

export interface OnboardWizardProps {
  /** Called when onboarding completes; parent should navigate to master view. */
  onComplete: () => void;
  /** Called when the user wants to skip onboarding for now. */
  onSkip: () => void;
}

export function OnboardWizard({ onComplete, onSkip }: OnboardWizardProps) {
  const [step, setStep] = useState<OnboardStep>('upload');
  const [runId, setRunId] = useState<string | null>(null);
  const [log, setLog] = useState<LogLine[]>([]);
  const [gapQuestions, setGapQuestions] = useState<GapQuestion[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const esRef = useRef<EventSource | null>(null);

  // Close SSE on unmount.
  useEffect(() => {
    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, []);

  function appendLog(msg: string, kind: LogLine['kind'] = 'info') {
    setLog((prev) => [...prev.slice(-200), { ts: Date.now(), msg, kind }]);
  }

  const subscribeToEvents = useCallback((currentRunId: string) => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }

    const url = buildOnboardEventsUrl(currentRunId);
    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener('transcript', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data as string) as {
          run_id?: string;
          payload?: Record<string, unknown>;
        };
        // Only process events for our run_id.
        if (data.run_id && data.run_id !== currentRunId) return;

        const payload = data.payload ?? {};
        // The supervisor serializes the event kind as `payload.type`
        // (see _pipeline_event_to_payload); fall back to `kind` defensively.
        const kind = String(payload.type ?? payload.kind ?? '');
        const message = String(payload.message ?? payload.msg ?? '');

        if (kind === 'gap_questions') {
          // Gap interview questions arrived — parse and transition.
          const questions = (payload.questions ?? []) as GapQuestion[];
          setGapQuestions(questions);
          setLog((prev) => [
            ...prev,
            { ts: Date.now(), msg: `gap questions received (${questions.length})`, kind: 'gap' },
          ]);
          setStep('gap');
          // Keep SSE open so the rest of the pipeline events can arrive after answers.
          return;
        }

        if (kind === 'phase_complete' && payload.phase === 'onboard') {
          appendLog('onboard pipeline complete', 'phase');
          setStep('done');
          if (esRef.current) {
            esRef.current.close();
            esRef.current = null;
          }
          return;
        }

        if (kind === 'phase_start' || kind === 'phase_complete') {
          appendLog(`[${String(payload.phase)}] ${message}`, 'phase');
          return;
        }

        if (message) {
          appendLog(message, 'info');
        }
      } catch { /* ignore malformed */ }
    });

    es.addEventListener('phase', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data as string) as {
          run_id?: string;
          phase?: string;
          status?: string;
        };
        if (data.run_id && data.run_id !== currentRunId) return;
        const phaseMsg = `phase ${data.phase ?? '?'} → ${data.status ?? '?'}`;
        appendLog(phaseMsg, 'phase');

        // Only the terminal "onboard" phase completion ends the flow. A
        // subphase done (ingest/merge) must NOT close the stream — otherwise
        // gap questions, which arrive after ingest, are never handled.
        const isTerminal = data.phase === 'onboard';
        if (isTerminal && (data.status === 'done' || data.status === 'backfilled')) {
          setStep('done');
          if (esRef.current) {
            esRef.current.close();
            esRef.current = null;
          }
        } else if (data.status === 'failed') {
          setErrorMsg(`Pipeline phase "${data.phase}" failed.`);
          setStep('error');
          if (esRef.current) {
            esRef.current.close();
            esRef.current = null;
          }
        }
      } catch { /* ignore */ }
    });

    es.onerror = () => {
      // SSE connection error — the supervisor may have closed the stream
      // naturally after completion. Only treat as error if still running.
      appendLog('SSE connection closed', 'info');
    };
  }, []);

  async function handleLaunch(formData: FormData) {
    setStep('running');
    setLog([]);
    setErrorMsg(null);
    appendLog('starting onboarding pipeline…', 'phase');
    try {
      const created = await postOnboard(formData);
      setRunId(created.run_id);
      appendLog(`run registered: ${created.run_id.slice(0, 8)}…`, 'info');
      subscribeToEvents(created.run_id);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMsg(msg);
      setStep('error');
    }
  }

  async function handleGapSubmit(answers: Record<string, string>) {
    if (!runId) throw new Error('No active run');
    await postOnboardAnswers(runId, answers);
    appendLog('answers submitted — pipeline continuing…', 'info');
    setStep('running');
  }

  // Step indicator
  const stepLabels: { id: OnboardStep; label: string }[] = [
    { id: 'upload', label: '1. upload' },
    { id: 'running', label: '2. ingesting' },
    { id: 'gap', label: '3. gap questions' },
    { id: 'done', label: '4. ready' },
  ];
  const stepOrder = ['upload', 'running', 'gap', 'done'];
  const currentIdx = stepOrder.indexOf(step);

  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1>onboarding</h1>
          <p>build your master content — the source of truth for all your applications.</p>
        </div>
        <div className="actions">
          {step !== 'done' && step !== 'error' && (
            <button
              type="button"
              className="btn ghost"
              onClick={onSkip}
            >
              skip for now
            </button>
          )}
        </div>
      </div>

      {/* Step indicator */}
      <div style={{ display: 'flex', gap: 0, marginBottom: 24, borderBottom: '1px solid var(--border)', paddingBottom: 16 }}>
        {stepLabels.map(({ id, label }, i) => {
          const isDone = stepOrder.indexOf(id) < currentIdx;
          const isCurrent = id === step || (step === 'error' && id === 'running');
          return (
            <div
              key={id}
              style={{
                fontSize: 12,
                fontFamily: 'var(--font-mono)',
                color: isDone ? 'var(--success)' : isCurrent ? 'var(--fg)' : 'var(--fg-subtle)',
                paddingRight: 24,
                display: 'flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              {isDone
                ? <Icon name="check" size={11} style={{ color: 'var(--success)' }} />
                : <span style={{
                    width: 16, height: 16, borderRadius: '50%', display: 'inline-flex',
                    alignItems: 'center', justifyContent: 'center', fontSize: 10,
                    background: isCurrent ? 'var(--fg)' : 'var(--bg-sunk)',
                    color: isCurrent ? 'var(--bg)' : 'var(--fg-subtle)',
                    border: '1px solid var(--border)',
                  }}>
                    {i + 1}
                  </span>
              }
              {label}
            </div>
          );
        })}
      </div>

      <div className="card" style={{ maxWidth: 640 }}>
        <div className="card-h">
          <h3>
            {step === 'upload' && 'provide your profile'}
            {step === 'running' && 'pipeline running'}
            {step === 'gap' && 'fill in the gaps'}
            {step === 'done' && 'masters ready'}
            {step === 'error' && 'onboarding failed'}
          </h3>
        </div>
        <div style={{ padding: '18px 20px' }}>
          {step === 'upload' && (
            <UploadStep onLaunch={handleLaunch} />
          )}
          {step === 'running' && runId && (
            <RunningStep
              runId={runId}
              log={log}
            />
          )}
          {step === 'gap' && (
            <GapStep
              questions={gapQuestions}
              onSubmit={handleGapSubmit}
            />
          )}
          {step === 'done' && (
            <DoneStep onFinish={onComplete} />
          )}
          {step === 'error' && (
            <div>
              <div
                style={{ padding: '12px 14px', background: 'var(--bg-elev)', border: '1px solid var(--error, #e55)', borderRadius: 'var(--radius)', marginBottom: 16, color: 'var(--error, #e55)', fontSize: 13 }}
                role="alert"
              >
                <div style={{ fontWeight: 600, marginBottom: 4 }}>onboarding failed</div>
                {errorMsg ?? 'An unexpected error occurred.'}
              </div>
              {log.length > 0 && (
                <div
                  style={{
                    background: 'var(--bg-sunk)',
                    border: '1px solid var(--border)',
                    borderRadius: 'var(--radius)',
                    padding: '10px 14px',
                    fontFamily: 'var(--font-mono)',
                    fontSize: 12,
                    lineHeight: 1.7,
                    maxHeight: 200,
                    overflowY: 'auto',
                    color: 'var(--fg-muted)',
                    marginBottom: 16,
                  }}
                >
                  {log.map((l, i) => <div key={i}>{l.msg}</div>)}
                </div>
              )}
              <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <button type="button" className="btn ghost" onClick={onSkip}>skip for now</button>
                <button type="button" className="btn primary" onClick={() => { setStep('upload'); setLog([]); setErrorMsg(null); }}>
                  try again
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
