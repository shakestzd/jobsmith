// new.tsx — port of design/app/new.jsx
//
// Pixel-identical DOM structure and class names. Prop shape derived from how
// main.jsx (design layer) invokes NewApplicationModal:
//   <NewApplicationModal onClose={() => setShowNew(false)} onLaunch={(slug) => { ... }} />

import { useState } from 'react';
import { Icon } from './shared';

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

export function NewApplicationModal({ onClose, onLaunch }: NewApplicationModalProps) {
  const [step, setStep] = useState<1 | 2>(1);
  const [url, setUrl] = useState('https://linear.app/careers/product-engineer');
  const [jdMode, setJdMode] = useState<JdMode>('fetch');
  const [jdText, setJdText] = useState('');
  const [verbose, setVerbose] = useState<VerbosityFlag>('-v');
  const [skipConfirm, setSkipConfirm] = useState(true);

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
    verbose ? `    ${verbose}` : null,
    skipConfirm ? `    --yes` : null,
  ]
    .filter(Boolean)
    .join(' \\\n');

  function handleBackdropClick(e: React.MouseEvent<HTMLDivElement>) {
    if (e.target === e.currentTarget) onClose();
  }

  function handleDialogKeyDown(e: React.KeyboardEvent<HTMLElement>) {
    if (e.key === 'Escape') onClose();
  }

  return (
    <div className="modal-backdrop" onClick={handleBackdropClick}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="new application"
        style={{ width: 620 }}
        onKeyDown={handleDialogKeyDown}
        tabIndex={-1}
      >
        <div className="modal-h">
          <h2>new application</h2>
          <span className="sub mono-sm" style={{ color: 'var(--fg-subtle)', marginLeft: 10 }}>step {step} of 2</span>
          <button type="button" className="btn ghost sm close" onClick={onClose}><Icon name="x" size={12} /></button>
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

        <div className="modal-foot">
          {step === 2 && (
            <button type="button" className="btn ghost" onClick={() => setStep(1)}>back</button>
          )}
          <button type="button" className="btn ghost" onClick={onClose}>cancel</button>
          {step === 1
            ? <button type="button" className="btn primary" onClick={() => setStep(2)}>review →</button>
            : <button type="submit" className="btn primary" onClick={() => onLaunch(slug)}><Icon name="play" size={12} /> apply</button>
          }
        </div>
      </div>
    </div>
  );
}
