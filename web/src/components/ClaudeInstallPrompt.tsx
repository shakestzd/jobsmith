// ClaudeInstallPrompt.tsx — desktop first-run `claude` CLI detection prompt
// (feat-dac00175, slice 6).
//
// Desktop-only. On mount it checks GET /api/desktop/deps/status:
//   - claude_installed   → renders nothing (CLI already present)
//   - 404 (not desktop)  → renders nothing (a normal web server has no
//                          /api/desktop/* routes — see main.py gating)
//   - not installed      → shows guided install instructions for Anthropic's
//                          no-Node native installer + a "Re-check" button.
//
// Unlike the browser download (which jobsmith can run itself), installing the
// `claude` CLI requires a shell command the user runs in their own terminal —
// so this prompt only instructs + re-probes; it never shells out.

import { useCallback, useEffect, useState } from 'react';
import { getDepsStatus, JobsmithApiError } from '../api/client';

// The official native installer (no Node.js / npm required). Verified against
// code.claude.com/docs/en/setup — the recommended default install method.
const INSTALL_COMMAND = 'curl -fsSL https://claude.ai/install.sh | bash';

type Phase = 'checking' | 'hidden' | 'needed' | 'rechecking' | 'installed';

export interface ClaudeInstallPromptProps {
  /** Optional CSS class for styling. */
  className?: string;
}

export function ClaudeInstallPrompt({ className = '' }: ClaudeInstallPromptProps) {
  const [phase, setPhase] = useState<Phase>('checking');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const probe = useCallback(async (recheck: boolean) => {
    setPhase(recheck ? 'rechecking' : 'checking');
    setErrorMsg(null);
    try {
      const status = await getDepsStatus();
      if (status.claude_installed) {
        // On a re-check show a brief confirmation; on first load just hide.
        setPhase(recheck ? 'installed' : 'hidden');
      } else {
        setPhase('needed');
      }
    } catch (err: unknown) {
      // 404 = not a desktop build → silently hide. Any other failure also
      // hides on first load (non-blocking); on an explicit re-check we keep the
      // prompt visible and surface the message so the user can retry.
      if (err instanceof JobsmithApiError && err.status === 404) {
        setPhase('hidden');
        return;
      }
      const msg = err instanceof Error ? err.message : 'Status check failed';
      if (recheck) {
        setErrorMsg(msg);
        setPhase('needed');
      } else {
        // eslint-disable-next-line no-console
        console.warn('claude deps status check failed', msg);
        setPhase('hidden');
      }
    }
  }, []);

  // Initial status probe.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      if (cancelled) return;
      await probe(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [probe]);

  const recheck = useCallback(() => {
    void probe(true);
  }, [probe]);

  if (phase === 'checking' || phase === 'hidden') {
    return null;
  }

  if (phase === 'installed') {
    return (
      <div
        className={className}
        role="region"
        aria-label="Claude CLI ready"
        style={{
          background: 'oklch(0.97 0.03 150)',
          border: '1px solid oklch(0.7 0.1 150)',
          borderRadius: 'var(--radius, 8px)',
          padding: '12px 16px',
          fontSize: 13,
          marginBottom: 16,
          color: 'oklch(0.35 0.08 150)',
        }}
      >
        Claude Code CLI detected — the apply pipeline is ready.
      </div>
    );
  }

  return (
    <div
      className={className}
      role="region"
      aria-label="Claude CLI required"
      style={{
        background: 'oklch(0.97 0.03 70)',
        border: '1px solid oklch(0.7 0.12 70)',
        borderRadius: 'var(--radius, 8px)',
        padding: '12px 16px',
        fontSize: 13,
        marginBottom: 16,
        color: 'oklch(0.35 0.08 70)',
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        Claude Code CLI required
      </div>
      <div style={{ fontSize: 12, marginBottom: 8 }}>
        Jobsmith drives its apply pipeline through Anthropic&rsquo;s{' '}
        <code>claude</code> command-line tool, which was not found on your PATH.
        Install it with the native installer (no Node.js required), then click
        Re-check:
      </div>

      <pre
        data-testid="claude-install-command"
        style={{
          margin: '0 0 8px',
          padding: 8,
          fontSize: 12,
          background: 'oklch(0.99 0.005 70)',
          borderRadius: 4,
          overflowX: 'auto',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}
      >
        {INSTALL_COMMAND}
      </pre>

      <div style={{ fontSize: 11, marginBottom: 8 }}>
        After it finishes, verify with <code>claude --version</code> in the same
        terminal. See{' '}
        <a
          href="https://code.claude.com/docs/en/setup"
          target="_blank"
          rel="noreferrer"
          style={{ color: 'oklch(0.45 0.12 70)' }}
        >
          the Claude Code setup docs
        </a>{' '}
        for Windows / alternative methods.
      </div>

      <button
        type="button"
        onClick={recheck}
        disabled={phase === 'rechecking'}
        style={{
          padding: '6px 14px',
          borderRadius: 'var(--radius, 6px)',
          border: '1px solid oklch(0.55 0.14 70)',
          background: phase === 'rechecking' ? 'oklch(0.85 0.05 70)' : 'oklch(0.6 0.14 70)',
          color: 'white',
          fontSize: 13,
          cursor: phase === 'rechecking' ? 'default' : 'pointer',
        }}
      >
        {phase === 'rechecking' ? 'Checking…' : 'Re-check'}
      </button>

      {errorMsg && (
        <div style={{ fontSize: 12, marginTop: 6 }}>{errorMsg}</div>
      )}
    </div>
  );
}

export default ClaudeInstallPrompt;
