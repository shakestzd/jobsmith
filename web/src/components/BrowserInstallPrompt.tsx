// BrowserInstallPrompt.tsx — desktop first-run Chromium download prompt
// (feat-0c74180d, slice 4).
//
// Desktop-only. On mount it checks GET /api/desktop/browser/status:
//   - installed          → renders nothing (browser already present)
//   - 404 (not desktop)  → renders nothing (a normal web server has no
//                          /api/desktop/* routes — see main.py gating)
//   - not installed      → shows a prompt with a "Download browser" button
//
// Clicking download POSTs /install and opens an EventSource on
// /install/events to stream progress. Progress lines are redacted before they
// hit the DOM (the SSE URL carries a ?token=).

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getBrowserStatus,
  installBrowser,
  buildBrowserInstallEventsUrl,
  redactSensitive,
  JobsmithApiError,
} from '../api/client';

type Phase = 'checking' | 'hidden' | 'needed' | 'installing' | 'done' | 'error';

export interface BrowserInstallPromptProps {
  /** Optional CSS class for styling. */
  className?: string;
}

interface ProgressEvent {
  phase?: string;
  message?: string;
  installed?: boolean;
}

export function BrowserInstallPrompt({ className = '' }: BrowserInstallPromptProps) {
  const [phase, setPhase] = useState<Phase>('checking');
  const [log, setLog] = useState<string[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  // Initial status probe.
  useEffect(() => {
    let cancelled = false;
    getBrowserStatus()
      .then((status) => {
        if (cancelled) return;
        setPhase(status.installed ? 'hidden' : 'needed');
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // 404 = not a desktop build → silently hide. Any other failure also
        // hides (non-blocking): the JD fetcher still degrades gracefully.
        if (err instanceof JobsmithApiError && err.status !== 404) {
          // eslint-disable-next-line no-console
          console.warn('browser status check failed', err.message);
        }
        setPhase('hidden');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Tear down the SSE connection on unmount.
  useEffect(() => {
    return () => {
      esRef.current?.close();
      esRef.current = null;
    };
  }, []);

  const appendLine = useCallback((line: string) => {
    setLog((prev) => [...prev.slice(-100), redactSensitive(line)]);
  }, []);

  const startInstall = useCallback(async () => {
    setPhase('installing');
    setLog([]);
    setErrorMsg(null);
    try {
      await installBrowser();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Install request failed';
      setErrorMsg(msg);
      setPhase('error');
      return;
    }

    esRef.current?.close();
    const es = new EventSource(buildBrowserInstallEventsUrl());
    esRef.current = es;

    es.addEventListener('progress', (e: MessageEvent) => {
      try {
        const data = JSON.parse(e.data as string) as ProgressEvent;
        if (data.phase === 'done') {
          setPhase('done');
          es.close();
          esRef.current = null;
        } else if (data.phase === 'error') {
          setErrorMsg(data.message ?? 'Download failed');
          setPhase('error');
          es.close();
          esRef.current = null;
        } else if (data.message) {
          // Non-terminal progress lines feed the log; terminal phases use the
          // dedicated done/error UI so the message is not shown twice.
          appendLine(data.message);
        }
      } catch {
        /* ignore malformed event */
      }
    });

    es.onerror = () => {
      // Connection dropped — surface a retryable error unless we already
      // reached a terminal state.
      setPhase((p) => (p === 'installing' ? 'error' : p));
      setErrorMsg((m) => m ?? 'Lost connection to the download stream.');
      es.close();
      esRef.current = null;
    };
  }, [appendLine]);

  if (phase === 'checking' || phase === 'hidden') {
    return null;
  }

  return (
    <div
      className={className}
      role="region"
      aria-label="Browser download"
      style={{
        background: 'oklch(0.97 0.03 250)',
        border: '1px solid oklch(0.7 0.1 250)',
        borderRadius: 'var(--radius, 8px)',
        padding: '12px 16px',
        fontSize: 13,
        marginBottom: 16,
        color: 'oklch(0.35 0.08 250)',
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        Browser download required
      </div>
      <div style={{ fontSize: 12, marginBottom: 8 }}>
        Some job sites render their description with JavaScript. Jobsmith needs a
        one-time Chromium download (~150&nbsp;MB) to read those pages. Until then
        it falls back to a faster fetch that may miss JS-rendered content.
      </div>

      {phase === 'needed' && (
        <button
          type="button"
          onClick={startInstall}
          style={{
            padding: '6px 14px',
            borderRadius: 'var(--radius, 6px)',
            border: '1px solid oklch(0.55 0.14 250)',
            background: 'oklch(0.6 0.14 250)',
            color: 'white',
            fontSize: 13,
            cursor: 'pointer',
          }}
        >
          Download browser
        </button>
      )}

      {phase === 'installing' && (
        <div style={{ fontSize: 12 }}>Downloading Chromium…</div>
      )}

      {phase === 'done' && (
        <div style={{ fontSize: 12, fontWeight: 500 }}>
          Browser installed — JS-rendered pages are now supported.
        </div>
      )}

      {phase === 'error' && (
        <div style={{ fontSize: 12 }}>
          <div style={{ fontWeight: 500 }}>Download failed.</div>
          {errorMsg && <div>{errorMsg}</div>}
          <button
            type="button"
            onClick={startInstall}
            style={{
              marginTop: 6,
              padding: '4px 12px',
              borderRadius: 'var(--radius, 6px)',
              border: '1px solid oklch(0.55 0.14 250)',
              background: 'transparent',
              color: 'oklch(0.4 0.12 250)',
              fontSize: 12,
              cursor: 'pointer',
            }}
          >
            Retry
          </button>
        </div>
      )}

      {log.length > 0 && (phase === 'installing' || phase === 'error') && (
        <pre
          data-testid="install-log"
          style={{
            marginTop: 8,
            maxHeight: 120,
            overflow: 'auto',
            fontSize: 11,
            background: 'oklch(0.99 0.005 250)',
            padding: 8,
            borderRadius: 4,
            whiteSpace: 'pre-wrap',
          }}
        >
          {log.join('\n')}
        </pre>
      )}
    </div>
  );
}

export default BrowserInstallPrompt;
