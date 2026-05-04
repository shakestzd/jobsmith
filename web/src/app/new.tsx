// new.tsx — port of design/app/new.jsx
//
// Pixel-identical DOM structure and class names. Prop shape derived from how
// main.jsx (design layer) invokes NewApplicationModal:
//   <NewApplicationModal onClose={() => setShowNew(false)} onLaunch={(slug) => { ... }} />
//
// Slice 4 (feat-7784ef64) wires the step-2 "apply" button to
// `useCreateApplication`. Behavior:
//   - JD source is one-of: fetched URL, pasted text, or uploaded file
//     (read with FileReader and base-64 encoded client-side).
//   - 4xx errors surface inline at the bottom of the modal body.
//   - On success the parent's `onLaunch(slug)` runs — main.tsx uses that
//     to set openSlug, which lands the user on the application detail
//     view with the (default) pipeline tab so the SSE stream picks up.

import { useState } from 'react';
import { Icon } from './shared';
import { useCreateApplication } from '../api/hooks';
import type { ApiVerbosity, CreateApplicationRequest } from '../api/types';
import { ApiError } from '../api/client';

// ── Prop interface ───────────────────────────────────────────────────────────

export interface NewApplicationModalProps {
  /** Called when the user dismisses the modal (cancel or backdrop click). */
  onClose: () => void;
  /** Called with the slug returned by the backend after a successful POST. */
  onLaunch: (slug: string) => void;
}

// ── Internal types ───────────────────────────────────────────────────────────

type JdMode = 'fetch' | 'paste' | 'file';
type VerbosityFlag = '' | '-v' | '-vv';

// ── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Read a File and return its base-64 encoded body (without the data: prefix).
 * Uses FileReader for compatibility with browsers — atob/btoa would require
 * us to manually slurp the bytes through a TextDecoder, which is fragile for
 * binary inputs (.pdf, .docx). Browser will reject memory-prohibitive files
 * upstream; we surface any read errors as an ApiError-shaped string.
 */
function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== 'string') {
        reject(new Error('FileReader returned non-string result'));
        return;
      }
      // Strip the "data:<mime>;base64," prefix to send only the payload.
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error ?? new Error('file read failed'));
    reader.readAsDataURL(file);
  });
}

/** Map the modal's verbosity dropdown to the API enum (empty → '-v'). */
function toApiVerbosity(v: VerbosityFlag): ApiVerbosity {
  if (v === '-vv') return '-vv';
  return '-v';
}

/** Best-effort parse of an ApiError body to find a `detail` string. */
function detailFromError(err: unknown): string {
  if (err instanceof ApiError) {
    try {
      const parsed = JSON.parse(err.body) as { detail?: unknown };
      if (typeof parsed.detail === 'string') return parsed.detail;
    } catch {
      // not JSON — fall through to body text
    }
    return err.body || err.message;
  }
  if (err instanceof Error) return err.message;
  return 'unknown error';
}

// ── Component ────────────────────────────────────────────────────────────────

export function NewApplicationModal({ onClose, onLaunch }: NewApplicationModalProps) {
  const [step, setStep] = useState<1 | 2>(1);
  const [url, setUrl] = useState('https://linear.app/careers/product-engineer');
  const [jdMode, setJdMode] = useState<JdMode>('fetch');
  const [jdText, setJdText] = useState('');
  const [jdFile, setJdFile] = useState<File | null>(null);
  const [verbose, setVerbose] = useState<VerbosityFlag>('-v');
  const [skipConfirm, setSkipConfirm] = useState(true);
  // Local validation error (e.g. missing input) — distinct from server errors.
  const [localError, setLocalError] = useState<string | null>(null);

  const createMut = useCreateApplication();

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

  const commandPreview = [
    `$ jobsmith apply '${url}'`,
    jdMode === 'paste' ? `    --jd-text-file /tmp/jd-${slug}.txt` : null,
    jdMode === 'file' && jdFile ? `    --jd-file '${jdFile.name}'` : null,
    verbose ? `    ${verbose}` : null,
    skipConfirm ? `    --yes` : null,
  ]
    .filter(Boolean)
    .join(' \\\n');

  const submitting = createMut.isPending;
  const serverErrorMsg = createMut.isError ? detailFromError(createMut.error) : null;
  const errorMsg = localError ?? serverErrorMsg;

  async function handleApply() {
    setLocalError(null);
    createMut.reset();

    // Build the per-source field — exactly one of jd_url / jd_text / jd_file_b64
    // is populated. Validate the selected source has content.
    let jd_url: string | null = null;
    let jd_text: string | null = null;
    let jd_file_b64: string | null = null;

    if (jdMode === 'fetch') {
      if (!url.trim()) {
        setLocalError('job url is required when fetching from url');
        return;
      }
      jd_url = url.trim();
    } else if (jdMode === 'paste') {
      if (!jdText.trim()) {
        setLocalError('jd text is required when pasting');
        return;
      }
      jd_text = jdText;
      // jd_url stays null — backend rejects requests with more than one
      // source set. The URL field is a hint to the user only in this mode.
    } else if (jdMode === 'file') {
      if (!jdFile) {
        setLocalError('please choose a file to upload');
        return;
      }
      try {
        jd_file_b64 = await readFileAsBase64(jdFile);
      } catch (e) {
        setLocalError(
          `failed to read file: ${e instanceof Error ? e.message : 'unknown error'}`,
        );
        return;
      }
      // jd_url stays null — see paste branch above.
    }

    const body: CreateApplicationRequest = {
      jd_url,
      jd_text,
      jd_file_b64,
      verbosity: toApiVerbosity(verbose),
      skip_confirmations: skipConfirm,
      force: false,
    };

    createMut.mutate(body, {
      onSuccess: (resp) => {
        onLaunch(resp.slug);
      },
    });
  }

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
                  type="file"
                  accept=".txt,.md,.pdf,.html,.htm"
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
                    const f = e.target.files?.[0] ?? null;
                    setJdFile(f);
                  }}
                />
                <div className="help">
                  {jdFile
                    ? `selected: ${jdFile.name} (${Math.round(jdFile.size / 1024)} KB)`
                    : 'pdf, html, txt, or markdown — base-64 encoded for transport.'}
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

            {errorMsg && (
              <div
                role="alert"
                style={{
                  marginTop: 14,
                  padding: '10px 12px',
                  background: 'var(--bg-sunk)',
                  border: '1px solid var(--danger, #c43)',
                  borderRadius: 'var(--radius)',
                  color: 'var(--danger, #c43)',
                  fontSize: 12.5,
                }}
              >
                <div className="mono-sm" style={{ marginBottom: 2 }}>could not start application</div>
                <div style={{ color: 'var(--fg-muted)' }}>{errorMsg}</div>
              </div>
            )}
          </div>
        )}

        <div className="modal-foot">
          {step === 2 && (
            <button className="btn ghost" onClick={() => setStep(1)} disabled={submitting}>back</button>
          )}
          <button className="btn ghost" onClick={onClose} disabled={submitting}>cancel</button>
          {step === 1
            ? <button className="btn primary" onClick={() => setStep(2)}>review →</button>
            : <button className="btn primary" onClick={handleApply} disabled={submitting}>
                {submitting ? <><span className="spin" /> applying…</> : <><Icon name="play" size={12} /> apply</>}
              </button>
          }
        </div>
      </div>
    </div>
  );
}
