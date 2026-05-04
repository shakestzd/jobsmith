// Live pipeline event-stream hook (slice 8 / feat-440324f1).
//
// `useEventStream(slug, opts?)` opens a long-lived EventSource against
// `GET /api/applications/{slug}/events` and surfaces:
//
//   { events: PipelineEvent[], status: ConnectionStatus }
//
// Where `PipelineEvent` is a tagged union over the two server event types
// (phase + specialist). The hook handles:
//
//  - reconnection with exponential backoff (1s → 2s → 4s … capped at 30s)
//  - cleanup on unmount or slug change (closes the EventSource)
//  - last-N event ring buffer (default 200)
//  - verbosity passthrough as a query parameter
//
// Kept in `web/src/api/events.ts` (not folded into `hooks.ts`) so the SSE
// concern stays separable — adding more streams later is mechanical.

import { useCallback, useEffect, useRef, useState } from 'react';

import { API_BASE_URL } from './client';

// ── Public types ─────────────────────────────────────────────────────────

export type Verbosity = 'quiet' | 'normal' | 'verbose';

export type ConnectionStatus = 'connecting' | 'open' | 'closed' | 'error';

export interface PhaseEventData {
  run_id: string;
  phase: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  rowid: number;
}

export interface SpecialistEventData {
  run_id: string;
  specialist: string;
  kind: string;
  phase: string | null;
  status: string | null;
  finished_at: string | null;
  transcript_ref: string | null;
  rowid: number;
}

export type PipelineEvent =
  | { kind: 'phase'; data: PhaseEventData; receivedAt: string }
  | { kind: 'specialist'; data: SpecialistEventData; receivedAt: string };

export interface UseEventStreamOptions {
  /** Server-side filter — quiet | normal | verbose. Default: 'verbose'. */
  verbosity?: Verbosity;
  /** Max events kept in memory. Default: 200. */
  maxEvents?: number;
  /** Disable the stream (useful when the parent has no slug yet). */
  enabled?: boolean;
}

export interface UseEventStreamResult {
  events: PipelineEvent[];
  status: ConnectionStatus;
}

// ── Constants ────────────────────────────────────────────────────────────

const DEFAULT_MAX_EVENTS = 200;
const RECONNECT_INITIAL_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

// ── Hook ─────────────────────────────────────────────────────────────────

export function useEventStream(
  slug: string | undefined,
  opts: UseEventStreamOptions = {},
): UseEventStreamResult {
  const verbosity: Verbosity = opts.verbosity ?? 'verbose';
  const maxEvents = opts.maxEvents ?? DEFAULT_MAX_EVENTS;
  const enabled = opts.enabled !== false && Boolean(slug);

  const [events, setEvents] = useState<PipelineEvent[]>([]);
  const [status, setStatus] = useState<ConnectionStatus>(
    enabled ? 'connecting' : 'closed',
  );

  // Refs so the effect body can access the latest counter without
  // re-subscribing on every state change.
  const sourceRef = useRef<EventSource | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectDelayRef = useRef<number>(RECONNECT_INITIAL_MS);

  const append = useCallback(
    (evt: PipelineEvent) => {
      setEvents(prev => {
        const next = [...prev, evt];
        return next.length > maxEvents
          ? next.slice(next.length - maxEvents)
          : next;
      });
    },
    [maxEvents],
  );

  useEffect(() => {
    // Reset transient state whenever the (slug, verbosity) pair changes.
    if (!enabled || !slug) {
      setStatus('closed');
      return;
    }

    // Wipe events whenever the slug actually changes — old events are stale.
    setEvents([]);
    setStatus('connecting');
    reconnectDelayRef.current = RECONNECT_INITIAL_MS;

    let cancelled = false;

    const cleanup = (): void => {
      if (sourceRef.current) {
        sourceRef.current.close();
        sourceRef.current = null;
      }
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const connect = (): void => {
      if (cancelled) return;

      const url = `${API_BASE_URL}/api/applications/${encodeURIComponent(
        slug,
      )}/events?verbosity=${encodeURIComponent(verbosity)}`;

      const es = new EventSource(url);
      sourceRef.current = es;

      es.onopen = () => {
        if (cancelled) return;
        setStatus('open');
        reconnectDelayRef.current = RECONNECT_INITIAL_MS;
      };

      es.addEventListener('phase', (rawEvt: Event) => {
        if (cancelled) return;
        const ev = rawEvt as MessageEvent;
        try {
          const data = JSON.parse(ev.data) as PhaseEventData;
          append({ kind: 'phase', data, receivedAt: new Date().toISOString() });
        } catch {
          // Malformed payload — drop silently rather than break the stream.
        }
      });

      es.addEventListener('specialist', (rawEvt: Event) => {
        if (cancelled) return;
        const ev = rawEvt as MessageEvent;
        try {
          const data = JSON.parse(ev.data) as SpecialistEventData;
          append({
            kind: 'specialist',
            data,
            receivedAt: new Date().toISOString(),
          });
        } catch {
          // Drop malformed payload.
        }
      });

      es.onerror = () => {
        if (cancelled) return;
        // EventSource is going to auto-retry on its own, but in some browsers
        // a hard error closes the connection. We schedule an explicit
        // reconnect with exponential backoff so the behaviour is predictable.
        setStatus('error');
        try {
          es.close();
        } catch {
          // ignore — already closed
        }
        sourceRef.current = null;

        const delay = reconnectDelayRef.current;
        reconnectDelayRef.current = Math.min(delay * 2, RECONNECT_MAX_MS);

        if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = setTimeout(() => {
          if (cancelled) return;
          setStatus('connecting');
          connect();
        }, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      cleanup();
      setStatus('closed');
    };
  }, [slug, verbosity, enabled, append]);

  return { events, status };
}
