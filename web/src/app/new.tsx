// new.tsx — port of design/app/new.jsx
//
// Pixel-identical DOM structure and class names. Prop shape derived from how
// main.jsx (design layer) invokes NewApplicationModal:
//   <NewApplicationModal onClose={() => setShowNew(false)} onLaunch={(slug) => { ... }} />

import { useRef, useState } from 'react';
import { Icon } from './shared';
import { useCreateApplication, type CreateApplicationBody } from '../api/hooks';
import { ApiError } from '../api/client';

// ── Prop interface ───────────────────────────────────────────────────────────

export interface NewApplicationModalProps {
  /** Called when the user dismisses the modal (cancel or backdrop click). */
  onClose: () => void;
  /** Called when the user confirms the application launch; receives the resolved slug. */
  onLaunch: (slug: string) => void;
}

// ── Internal types ───────────────────────────────────────────────────────────

type JdMode = 'fetch' | 'paste' | 'file';
type VerbosityFlag = '' | '-v' | '-vv';

// ── Component ────────────────────────────────────────────────────────────────

// Map UI verbosity flag → API verbosity word. The backend turns these
// human-readable tokens into CLI flags (-v / -vv / -vvv) inside
// api/applications.py:_verbosity_to_cli_flag.
function uiVerbosityToApi(v: VerbosityFlag): CreateApplicationBody['verbosity'] {
  if (v === '-vv') return 'debug';
  if (v === '-v') return 'verbose';
  return 'normal';
}

export function NewApplicationModal({ onClose, onLaunch }: NewApplicationModalProps) {
  const [step, setStep] = useState<1 | 2>(1);
  const [url, setUrl] = useState('https://linear.app/careers/product-engineer');
  const [jdMode, setJdMode] = useState<JdMode>('fetch');
  const [jdText, setJdText] = useState('');
  const [jdFile, setJdFile] = useState<File | null>(null);
  const [verbose, setVerbose] = useState<VerbosityFlag>('-v');
  const [skipConfirm, setSkipConfirm] = useState(true);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const create = useCreateApplication();

  const slug = (() => {
    try {
      const u = new URL(url);
      const host = u.hostname.replace(/^www\./, '').split('.')[0];
      const path = u.pathname.split('/').filter(Boolean).slice(-1)[0] || 'role';
      const date = new Date().toISOString().slice(0, 7);
      return `${host}-${path}-${date}`;
    } catch {
      return 'new-application';
    }
  })();

  const jdModeOptions: [JdMode, string][] = [
    ['fetch', 'fetch from url'],
    ['paste', 'paste text'],
    ['file', 'upload file'],
  ];

  const preflightItems: [string, string][] = [
    ['claude CLI', 'v1.4.0'],
    ['quarto', 'v1.5.57'],
    ['master/work.yml', '38 bullets · valid'],
    ['benchmark.md', 'present'],
    ['private/jobsmith.db', 'open · 7 prior runs'],
  ];

  // Mirror the argv the backend builds:
  //   - fetch:  jobsmith apply <url>
  //   - paste / file:  jobsmith apply file://placeholder --jd-text-file <path>
  // (file mode also writes a jd.txt in the slug dir; we show that path so
  // the preview matches the actual CLI invocation.)
  const previewUrl = jdMode === 'fetch' ? url : 'file://placeholder';
  const previewTextFile =
    jdMode === 'paste' ? `/tmp/jd-${slug}.txt`
    : jdMode === 'file' && jdFile ? `<${slug}>/jd.txt`
    : null;
  const commandPreview = [
    `$ jobsmith apply '${previewUrl}'`,
    previewTextFile ? `    --jd-text-file ${previewTextFile}` : null,
    verbose ? `    ${verbose}` : null,
    skipConfirm ? `    --yes` : null,
  ]
    .filter(Boolean)
    .join(' \\\n');

  // Read a File as base64 for the jd_file_b64 path.
  const readFileAsBase64 = (file: File): Promise<string> =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result;
        if (typeof result !== 'string') {
          reject(new Error('FileReader returned non-string result'));
          return;
        }
        // result is a data URL: "data:text/plain;base64,<payload>".
        const comma = result.indexOf(',');
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(reader.error ?? new Error('FileReader failed'));
      reader.readAsDataURL(file);
    });

  const handleApply = async () => {
    setSubmitError(null);
    try {
      const body: CreateApplicationBody = {
        skip_confirmations: skipConfirm,
        verbosity: uiVerbosityToApi(verbose),
      };
      if (jdMode === 'fetch') {
        body.jd_url = url;
      } else if (jdMode === 'paste') {
        if (!jdText.trim()) {
          setSubmitError('Paste the job description text or switch to fetch / upload.');
          return;
        }
        body.jd_text = jdText;
      } else if (jdMode === 'file') {
        if (!jdFile) {
          setSubmitError('Select a JD file or switch to fetch / paste.');
          return;
        }
        body.jd_file_b64 = await readFileAsBase64(jdFile);
      }
      const resp = await create.mutateAsync(body);
      onLaunch(resp.slug);
    } catch (err) {
      if (err instanceof ApiError) {
        // Surface the server's "detail" field if it's JSON; otherwise show the raw body.
        try {
          const parsed = JSON.parse(err.body);
          setSubmitError(typeof parsed.detail === 'string' ? parsed.detail : err.message);
        } catch {
          setSubmitError(err.body || err.message);
        }
      } else {
        setSubmitError(err instanceof Error ? err.message : String(err));
      }
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" style={{ width: 620 }} onClick={(e: React.MouseEvent<HTMLDivElement>) => e.stopPropagation()}>
        <div className="modal-h">
          <h2>new application</h2>
          <span className="sub mono-sm" style={{ color: 'var(--fg-subtle)', marginLeft: 10 }}>step {step} of 2</span>
          <button className="btn ghost sm close" onClick={onClose}><Icon name="x" size={12} /></button>
        </div>

        {step === 1 && (
          <div className="modal-body">
            <div className="field">
              <label>job url</label>
              <input
                className="mono"
                value={url}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setUrl(e.target.value)}
                placeholder="https://..."
              />
              <div className="help">jobsmith will derive the slug, fetch the JD, and resolve a starting state.</div>
            </div>

            <div className="field">
              <label>job description source</label>
              <div style={{ display: 'flex', gap: 6 }}>
                {jdModeOptions.map(([id, label]) => (
                  <span
                    key={id}
                    className={`pill ${jdMode === id ? 'active' : ''}`}
                    onClick={() => setJdMode(id)}
                  >
                    {label}
                  </span>
                ))}
              </div>
            </div>

            {jdMode === 'paste' && (
              <div className="field">
                <label>jd text</label>
                <textarea
                  className="mono"
                  value={jdText}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setJdText(e.target.value)}
                  placeholder="Paste the full job description here…"
                />
                <div className="help">written to a tempfile during the run; deleted after.</div>
              </div>
            )}

            {jdMode === 'file' && (
              <div className="field">
                <label>jd file</label>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".txt,.md"
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                    setJdFile(e.target.files?.[0] ?? null)
                  }
                />
                <div className="help">
                  {jdFile
                    ? `Selected: ${jdFile.name} (${jdFile.size} bytes)`
                    : 'Plain text or markdown only — backend decodes the upload as UTF-8.'}
                </div>
              </div>
            )}

            <div className="field" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
              <div>
                <label>verbosity</label>
                <select
                  value={verbose}
                  onChange={(e: React.ChangeEvent<HTMLSelectElement>) => setVerbose(e.target.value as VerbosityFlag)}
                >
                  <option value="">default</option>
                  <option value="-v">−v (steps)</option>
                  <option value="-vv">−vv (tools + payloads)</option>
                </select>
              </div>
              <div>
                <label>preflight</label>
                <select>
                  <option>doctor + lint</option>
                  <option>doctor only</option>
                  <option>skip</option>
                </select>
              </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 12px', background: 'var(--bg-sunk)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', marginTop: 6 }}>
              <input
                type="checkbox"
                id="yes"
                checked={skipConfirm}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSkipConfirm(e.target.checked)}
              />
              <label htmlFor="yes" style={{ flex: 1, fontSize: 13 }}>
                <span style={{ fontWeight: 500 }}>skip confirmations</span>
                <div style={{ color: 'var(--fg-muted)', fontSize: 12 }}>equivalent to <span className="mono-sm">−−yes</span>; phases run end-to-end without prompts.</div>
              </label>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="modal-body">
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>resolved slug</div>
            <div style={{ padding: '12px 14px', background: 'var(--bg-sunk)', border: '1px solid var(--border)', borderRadius: 'var(--radius)', marginBottom: 18 }}>
              <div className="mono-sm" style={{ color: 'var(--accent-soft-fg)', fontSize: 13 }}>{slug}</div>
            </div>

            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>command preview</div>
            <pre className="code" style={{ marginBottom: 18 }}>{commandPreview}</pre>

            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>preflight</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {preflightItems.map(([k, v]) => (
                <div key={k} style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '6px 10px', background: 'var(--bg-sunk)', border: '1px solid var(--border)', borderRadius: 'var(--radius)' }}>
                  <Icon name="check" size={12} style={{ color: 'var(--success)' }} />
                  <span className="mono-sm" style={{ flex: 1 }}>{k}</span>
                  <span className="mono-sm" style={{ color: 'var(--fg-muted)' }}>{v}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {submitError && step === 2 && (
          <div
            className="error-banner"
            role="alert"
            style={{
              margin: '0 16px 12px',
              padding: '10px 12px',
              borderRadius: 'var(--radius)',
              border: '1px solid var(--danger, #c33)',
              background: 'var(--bg-sunk)',
              color: 'var(--danger, #c33)',
              fontSize: 12.5,
            }}
          >
            {submitError}
          </div>
        )}

        <div className="modal-foot">
          {step === 2 && (
            <button
              className="btn ghost"
              onClick={() => setStep(1)}
              disabled={create.isPending}
            >
              back
            </button>
          )}
          <button
            className="btn ghost"
            onClick={onClose}
            disabled={create.isPending}
          >
            cancel
          </button>
          {step === 1 ? (
            <button className="btn primary" onClick={() => setStep(2)}>
              review →
            </button>
          ) : (
            <button
              className="btn primary"
              onClick={handleApply}
              disabled={create.isPending}
            >
              {create.isPending ? (
                <>queuing…</>
              ) : (
                <>
                  <Icon name="play" size={12} /> apply
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
