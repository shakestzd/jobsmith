// SSE consumer for the /api/applications/{slug}/events stream.
//
// PR #29's events.py emits two event types: `phase` and `specialist`. Each
// has a JSON payload. Heartbeats are sent as comments (`:ping`). Idle close
// is server-driven; this consumer auto-reconnects on close.
//
// Decision: we cannot use the browser's native EventSource because it does
// not let us set headers (no way to send Authorization: Bearer). Instead we
// use fetch + ReadableStream and parse SSE framing ourselves. This is the
// same pattern Anthropic's web-app SDK uses.

import { API_BASE_URL } from './client';
import type { LogEventData, PhaseEventData, SpecialistEventData } from './types';

export type SseEvent =
  | { type: 'phase'; data: PhaseEventData }
  | { type: 'specialist'; data: SpecialistEventData }
  | { type: 'log'; data: LogEventData };

export interface EventStreamHandlers {
  onEvent: (e: SseEvent) => void;
  onError?: (err: unknown) => void;
  onOpen?: () => void;
  /** Called once when the stream finally closes (either via abort or unrecoverable error). */
  onClose?: () => void;
}

export interface EventStreamOptions extends EventStreamHandlers {
  /** Server-side verbosity: 'quiet' | 'normal' | 'verbose'. Default 'verbose'. */
  verbosity?: 'quiet' | 'normal' | 'verbose';
  /** Token override (otherwise resolved from VITE_API_TOKEN). */
  token?: string;
  /** Reconnect on close? Default true. */
  reconnect?: boolean;
  /** Initial reconnect delay (ms). Doubles up to maxBackoffMs. */
  initialBackoffMs?: number;
  maxBackoffMs?: number;
}

interface ParsedSseFrame {
  event: string | null;
  data: string;
}

/** Parse SSE wire format: `event: <type>\ndata: <json>\n\n`. */
export function parseSseChunk(chunk: string): ParsedSseFrame[] {
  const frames: ParsedSseFrame[] = [];
  for (const block of chunk.split(/\n\n/)) {
    if (!block.trim()) continue;
    let event: string | null = null;
    const dataLines: string[] = [];
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue; // comment / heartbeat
      if (line.startsWith('event:')) {
        event = line.slice('event:'.length).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice('data:'.length).trim());
      }
    }
    if (dataLines.length > 0) {
      frames.push({ event, data: dataLines.join('\n') });
    }
  }
  return frames;
}

function authHeaders(token: string | undefined): Record<string, string> {
  const fromEnv =
    typeof import.meta !== 'undefined' && import.meta.env
      ? (import.meta.env.VITE_API_TOKEN as string | undefined)
      : undefined;
  const t = token ?? fromEnv ?? '';
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/**
 * Open an SSE stream. Returns a function that closes it.
 *
 * The returned closer aborts the underlying fetch and prevents auto-reconnect.
 */
export function openEventStream(
  slug: string,
  opts: EventStreamOptions,
): () => void {
  const verbosity = opts.verbosity ?? 'verbose';
  const reconnect = opts.reconnect ?? true;
  const initialBackoff = opts.initialBackoffMs ?? 1_000;
  const maxBackoff = opts.maxBackoffMs ?? 30_000;
  let backoff = initialBackoff;
  let aborted = false;
  let controller: AbortController | null = null;

  const url = `${API_BASE_URL}/api/applications/${encodeURIComponent(slug)}/events?verbosity=${verbosity}`;

  const dispatch = (frame: ParsedSseFrame) => {
    let data: unknown;
    try {
      data = JSON.parse(frame.data);
    } catch (err) {
      opts.onError?.(err);
      return;
    }
    // Per the SSE spec, a frame with only `data:` is a default-typed frame
    // (event = "message"). Surface it through onError as a notice rather
    // than silently dropping — if the backend ever forgets the `event:`
    // line we want a signal in the operator log.
    const eventType = frame.event ?? 'message';
    if (eventType === 'phase') {
      opts.onEvent({ type: 'phase', data: data as PhaseEventData });
    } else if (eventType === 'specialist') {
      opts.onEvent({ type: 'specialist', data: data as SpecialistEventData });
    } else if (eventType === 'log') {
      opts.onEvent({ type: 'log', data: data as LogEventData });
    } else {
      opts.onError?.(
        new Error(`SSE: unhandled event type ${JSON.stringify(eventType)}`),
      );
    }
  };

  const connect = async (): Promise<void> => {
    controller = new AbortController();
    const headers: Record<string, string> = {
      Accept: 'text/event-stream',
      ...authHeaders(opts.token),
    };
    let resp: Response;
    try {
      resp = await fetch(url, {
        method: 'GET',
        headers,
        signal: controller.signal,
      });
    } catch (err) {
      if (aborted) return;
      opts.onError?.(err);
      return scheduleReconnect();
    }

    if (!resp.ok) {
      opts.onError?.(new Error(`SSE ${url} → ${resp.status}`));
      return scheduleReconnect();
    }
    if (!resp.body) {
      opts.onError?.(new Error('SSE response missing body stream'));
      return scheduleReconnect();
    }

    opts.onOpen?.();
    backoff = initialBackoff; // reset on a successful connect

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // Frames are terminated by a blank line. Process each complete frame.
        const lastSep = buffer.lastIndexOf('\n\n');
        if (lastSep === -1) continue;
        const ready = buffer.slice(0, lastSep + 2);
        buffer = buffer.slice(lastSep + 2);
        for (const frame of parseSseChunk(ready)) dispatch(frame);
      }
    } catch (err) {
      if (!aborted) opts.onError?.(err);
    }
    return scheduleReconnect();
  };

  const scheduleReconnect = () => {
    if (aborted || !reconnect) {
      opts.onClose?.();
      return;
    }
    const delay = backoff;
    backoff = Math.min(backoff * 2, maxBackoff);
    setTimeout(() => {
      if (!aborted) void connect();
    }, delay);
  };

  void connect();

  return () => {
    aborted = true;
    controller?.abort();
    opts.onClose?.();
  };
}
