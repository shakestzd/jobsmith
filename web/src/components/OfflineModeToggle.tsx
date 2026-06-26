// OfflineModeToggle.tsx — desktop offline-mode LLM backend status + (deferred)
// enable control (feat-aaa91b6d, slice 7).
//
// Desktop-only. On mount it probes GET /api/desktop/llm/status:
//   - 404 (not desktop) → renders nothing (a normal web server has no
//                         /api/desktop/* routes — see main.py gating)
//   - other error       → renders nothing on first load (non-blocking)
//   - success           → shows per-backend (MLX / Ollama) reachability +
//                         runtime-installed status with a Re-check button.
//
// REDUCED SCOPE (plan-a23bba5f, slice 7): detection + status ONLY. The actual
// "enable offline mode" wiring — writing the pluggable-backend `llm` config and
// routing chat + scoring to a local server — is DEFERRED to plan-938f735b. The
// Enable button is shown but degrades loudly: clicking it surfaces the server's
// 501 "coming soon — pending plan-938f735b" notice. It never crashes or
// silently no-ops.

import { useCallback, useEffect, useState, type ReactNode } from 'react';
import {
  getLlmStatus,
  enableOfflineMode,
  JobsmithApiError,
  type LlmStatus,
  type LlmBackendStatus,
} from '../api/client';

type Phase = 'checking' | 'hidden' | 'ready';

interface BackendMeta {
  label: string;
  startHint: string; // command to start the server when the runtime is present
  installLabel: string;
  installHref: string;
}

const BACKENDS: Record<'mlx' | 'ollama', BackendMeta> = {
  mlx: {
    label: 'MLX',
    startHint: 'mlx_lm.server',
    installLabel: 'pip install mlx-lm',
    installHref: 'https://github.com/ml-explore/mlx-lm',
  },
  ollama: {
    label: 'Ollama',
    startHint: 'ollama serve',
    installLabel: 'Download Ollama',
    installHref: 'https://ollama.com/download',
  },
};

export interface OfflineModeToggleProps {
  /** Optional CSS class for styling. */
  className?: string;
}

function BackendRow({
  meta,
  status,
}: {
  meta: BackendMeta;
  status: LlmBackendStatus;
}) {
  let body: ReactNode;
  if (status.reachable) {
    body = (
      <span style={{ color: 'oklch(0.4 0.1 150)' }}>
        Server detected at <code>{status.base_url}</code>
        {status.model ? (
          <>
            {' '}— <code>{status.model}</code>
          </>
        ) : null}
      </span>
    );
  } else if (status.runtime_installed) {
    body = (
      <span style={{ color: 'oklch(0.45 0.1 70)' }}>
        Runtime installed but not running — start it with{' '}
        <code>{meta.startHint}</code>, then Re-check.
      </span>
    );
  } else {
    body = (
      <span style={{ color: 'oklch(0.5 0.02 260)' }}>
        Not detected.{' '}
        <a href={meta.installHref} target="_blank" rel="noreferrer">
          {meta.installLabel}
        </a>{' '}
        to run models locally.
      </span>
    );
  }
  return (
    <div
      data-testid={`offline-backend-${meta.label.toLowerCase()}`}
      style={{ fontSize: 12, marginBottom: 6 }}
    >
      <strong>{meta.label}:</strong> {body}
    </div>
  );
}

export function OfflineModeToggle({ className = '' }: OfflineModeToggleProps) {
  const [phase, setPhase] = useState<Phase>('checking');
  const [status, setStatus] = useState<LlmStatus | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [pendingMsg, setPendingMsg] = useState<string | null>(null);
  const [enabling, setEnabling] = useState(false);

  const probe = useCallback(async () => {
    setErrorMsg(null);
    try {
      const next = await getLlmStatus();
      setStatus(next);
      setPhase('ready');
    } catch (err: unknown) {
      // 404 = not a desktop build → silently hide. Any other failure also hides
      // on first load (non-blocking) — but if we are already showing the panel
      // (a Re-check), keep it visible and surface the message.
      if (err instanceof JobsmithApiError && err.status === 404) {
        setPhase('hidden');
        return;
      }
      const msg = err instanceof Error ? err.message : 'Status check failed';
      setErrorMsg(msg);
      setPhase((p) => (p === 'ready' ? 'ready' : 'hidden'));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      if (cancelled) return;
      await probe();
    })();
    return () => {
      cancelled = true;
    };
  }, [probe]);

  const onEnable = useCallback(async () => {
    setEnabling(true);
    setPendingMsg(null);
    try {
      const ack = await enableOfflineMode();
      // 501 placeholder: surface the server's reason so the user knows offline
      // routing is pending (not broken). Defensive fallback keeps it non-silent.
      setPendingMsg(ack.reason || 'Offline mode is pending plan-938f735b.');
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Enable failed';
      setPendingMsg(msg);
    } finally {
      setEnabling(false);
    }
  }, []);

  if (phase === 'checking' || phase === 'hidden') {
    return null;
  }

  return (
    <div
      className={className}
      role="region"
      aria-label="Offline mode"
      style={{
        background: 'oklch(0.98 0.01 260)',
        border: '1px solid oklch(0.8 0.03 260)',
        borderRadius: 'var(--radius, 8px)',
        padding: '12px 16px',
        fontSize: 13,
        marginBottom: 16,
        color: 'oklch(0.3 0.03 260)',
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 6 }}>
        Offline mode (local LLM)
      </div>

      {status && (
        <>
          <BackendRow meta={BACKENDS.mlx} status={status.mlx} />
          <BackendRow meta={BACKENDS.ollama} status={status.ollama} />
        </>
      )}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8 }}>
        <button
          type="button"
          onClick={() => void onEnable()}
          disabled={enabling}
          style={{
            padding: '6px 14px',
            borderRadius: 'var(--radius, 6px)',
            border: '1px solid oklch(0.55 0.05 260)',
            background: enabling ? 'oklch(0.85 0.03 260)' : 'oklch(0.6 0.06 260)',
            color: 'white',
            fontSize: 13,
            cursor: enabling ? 'default' : 'pointer',
          }}
        >
          {enabling ? 'Enabling…' : 'Enable offline mode'}
        </button>
        <button
          type="button"
          onClick={() => void probe()}
          style={{
            padding: '6px 12px',
            borderRadius: 'var(--radius, 6px)',
            border: '1px solid oklch(0.7 0.02 260)',
            background: 'transparent',
            color: 'oklch(0.4 0.03 260)',
            fontSize: 13,
            cursor: 'pointer',
          }}
        >
          Re-check
        </button>
      </div>

      <div style={{ fontSize: 11, marginTop: 6, color: 'oklch(0.5 0.02 260)' }}>
        Coming soon — routing chat &amp; scoring to a local backend is pending
        the pluggable-backend work (plan-938f735b).
      </div>

      {pendingMsg && (
        <div
          data-testid="offline-pending"
          role="status"
          style={{
            fontSize: 12,
            marginTop: 8,
            padding: '6px 10px',
            background: 'oklch(0.97 0.03 70)',
            border: '1px solid oklch(0.8 0.08 70)',
            borderRadius: 6,
            color: 'oklch(0.4 0.08 70)',
          }}
        >
          {pendingMsg}
        </div>
      )}

      {errorMsg && (
        <div style={{ fontSize: 12, marginTop: 6 }}>{errorMsg}</div>
      )}
    </div>
  );
}

export default OfflineModeToggle;
