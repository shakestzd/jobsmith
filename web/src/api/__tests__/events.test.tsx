// Tests for the live SSE event-stream hook (slice 8 / feat-440324f1).
//
// Coverage strategy: 3 representative cases —
//   1. lifecycle (status transitions connecting → open → closed on unmount)
//   2. event accumulation (specialist + phase events appended in order)
//   3. slug-change cleanup (old EventSource closed, new one opened)
//
// We stub the global EventSource. The browser's native EventSource is not
// implemented in jsdom, so this stub is the only sensible test seam.

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

import { useEventStream } from '../events';

// ── Stub EventSource ─────────────────────────────────────────────────────

type Listener = (ev: MessageEvent) => void;

interface StubInstance {
  url: string;
  readyState: number;
  close: () => void;
  // Helpers exposed for tests to drive the stub.
  __open: () => void;
  __emit: (eventName: string, data: unknown) => void;
  __error: () => void;
}

const __instances: StubInstance[] = [];

class StubEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;

  url: string;
  readyState = StubEventSource.CONNECTING;
  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onmessage: Listener | null = null;
  private listeners = new Map<string, Set<Listener>>();
  // Trace count of close() calls for cleanup assertions.
  closed = 0;

  constructor(url: string) {
    this.url = url;
    const self = this;
    __instances.push({
      url,
      get readyState() {
        return self.readyState;
      },
      close: () => self.close(),
      __open: () => {
        self.readyState = StubEventSource.OPEN;
        self.onopen?.(new Event('open'));
      },
      __emit: (eventName: string, data: unknown) => {
        const evt = new MessageEvent(eventName, {
          data: typeof data === 'string' ? data : JSON.stringify(data),
        });
        const set = self.listeners.get(eventName);
        if (set) for (const l of set) l(evt);
        if (eventName === 'message') self.onmessage?.(evt);
      },
      __error: () => {
        self.onerror?.(new Event('error'));
      },
    });
  }

  addEventListener(name: string, listener: Listener): void {
    let set = this.listeners.get(name);
    if (!set) {
      set = new Set();
      this.listeners.set(name, set);
    }
    set.add(listener);
  }

  removeEventListener(name: string, listener: Listener): void {
    this.listeners.get(name)?.delete(listener);
  }

  close(): void {
    this.readyState = StubEventSource.CLOSED;
    this.closed += 1;
  }
}

beforeEach(() => {
  __instances.length = 0;
  vi.stubGlobal('EventSource', StubEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// ── Tests ────────────────────────────────────────────────────────────────

describe('useEventStream', () => {
  it('transitions through connecting → open and closes on unmount', async () => {
    const { result, unmount } = renderHook(() =>
      useEventStream('alpha-co', { verbosity: 'normal' }),
    );

    expect(result.current.status).toBe('connecting');
    expect(__instances).toHaveLength(1);
    expect(__instances[0].url).toContain('/api/applications/alpha-co/events');
    expect(__instances[0].url).toContain('verbosity=normal');

    act(() => __instances[0].__open());
    await waitFor(() => expect(result.current.status).toBe('open'));

    unmount();
    // After unmount the EventSource must be closed (readyState=2).
    expect(__instances[0].readyState).toBe(2);
  });

  it('accumulates `specialist` and `phase` events in arrival order', async () => {
    const { result } = renderHook(() =>
      useEventStream('beta-co', { verbosity: 'verbose' }),
    );

    act(() => __instances[0].__open());
    await waitFor(() => expect(result.current.status).toBe('open'));

    act(() =>
      __instances[0].__emit('phase', {
        run_id: 'r1',
        phase: 'gather',
        status: 'running',
      }),
    );
    act(() =>
      __instances[0].__emit('specialist', {
        run_id: 'r1',
        kind: 'jd-parsed',
        specialist: 'apply-jd-parser',
        finished_at: '2026-05-04T12:00:01Z',
      }),
    );

    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.events[0].kind).toBe('phase');
    expect(result.current.events[1].kind).toBe('specialist');
    const second = result.current.events[1] as { data: { kind: string } };
    expect(second.data.kind).toBe('jd-parsed');
  });

  it('closes the old stream and opens a new one when slug changes', async () => {
    const { result, rerender } = renderHook(
      ({ slug }: { slug: string }) =>
        useEventStream(slug, { verbosity: 'normal' }),
      { initialProps: { slug: 'first-slug' } },
    );

    expect(__instances).toHaveLength(1);
    act(() => __instances[0].__open());
    await waitFor(() => expect(result.current.status).toBe('open'));

    rerender({ slug: 'second-slug' });

    // Old stream closed, new one opened pointing at the new slug.
    expect(__instances[0].readyState).toBe(2); // CLOSED
    expect(__instances).toHaveLength(2);
    expect(__instances[1].url).toContain('/api/applications/second-slug/events');
  });
});
