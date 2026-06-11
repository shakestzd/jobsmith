// application-timestamps.test.ts — unit tests for timestamp handling in event stream
//
// Assertions:
//   formatEventTime() converts ISO timestamps to HH:MM:SS or falls back to now()
//   pickPayloadTime() extracts ts from payload or returns now()
//   transcriptToLogEvent() uses stored timestamp instead of receipt time
//   Stored timestamps far in the past are preserved in LogEvent
//   Missing ts field falls back gracefully
//   parseIso() returns Date for valid ISO strings, null for absent/invalid
//   formatPhaseDuration() computes real elapsed time from fixture timestamps
//   formatPhaseDuration() returns '—' when either timestamp is missing/invalid

import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  formatEventTime,
  pickPayloadTime,
  transcriptToLogEvent,
  parseIso,
  formatPhaseDuration,
} from './application';
import type { SseTranscriptEvent } from './application';

describe('timestamp handling', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('formatEventTime', () => {
    it('formats valid ISO timestamp to HH:MM:SS', () => {
      const iso = '2026-05-06T10:30:45+00:00';
      const result = formatEventTime(iso);
      const expected = new Date(iso).toTimeString().slice(0, 8);
      expect(result).toBe(expected);
    });

    it('returns current time for null input', () => {
      const result = formatEventTime(null);
      // Verify it's a valid HH:MM:SS format by checking the pattern
      expect(result).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('returns current time for undefined input', () => {
      const result = formatEventTime(undefined);
      expect(result).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('returns current time for unparseable input', () => {
      const result = formatEventTime('invalid-date-string');
      expect(result).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('respects local timezone when formatting', () => {
      const iso = '2026-01-15T08:45:30+00:00';
      const result = formatEventTime(iso);
      const expected = new Date(iso).toTimeString().slice(0, 8);
      expect(result).toBe(expected);
    });
  });

  describe('pickPayloadTime', () => {
    it('extracts ts from payload when present', () => {
      const iso = '2026-05-06T10:30:45+00:00';
      const payload = { ts: iso, type: 'tool_call' };
      const result = pickPayloadTime(payload);
      const expected = new Date(iso).toTimeString().slice(0, 8);
      expect(result).toBe(expected);
    });

    it('returns current time when ts is absent', () => {
      const payload = { type: 'tool_call' };
      const result = pickPayloadTime(payload);
      expect(result).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('returns current time when ts is not a string', () => {
      const payload = { ts: 12345, type: 'tool_call' };
      const result = pickPayloadTime(payload);
      expect(result).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('returns current time when ts is unparseable', () => {
      const payload = { ts: 'garbage', type: 'tool_call' };
      const result = pickPayloadTime(payload);
      expect(result).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });
  });

  describe('transcriptToLogEvent', () => {
    it('preserves stored ts in phase_boundary event', () => {
      const iso = '2026-05-06T09:15:30+00:00';
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          ts: iso,
          _phase_boundary: 'gather',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.kind).toBe('phase_boundary');
      expect(result!.ts).toBe(new Date(iso).toTimeString().slice(0, 8));
    });

    it('preserves stored ts in tool_call event', () => {
      const iso = '2026-05-06T09:20:00+00:00';
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          ts: iso,
          type: 'tool_call',
          tool_name: 'search',
          tool_input_truncated: 'query: "test"',
          tool_use_id: 'use-123',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.kind).toBe('tool_call');
      expect(result!.ts).toBe(new Date(iso).toTimeString().slice(0, 8));
    });

    it('preserves stored ts in tool_result event', () => {
      const iso = '2026-05-06T09:21:15+00:00';
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          ts: iso,
          type: 'tool_result',
          result_truncated: 'found 5 results',
          tool_use_id: 'use-123',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.kind).toBe('tool_result');
      expect(result!.ts).toBe(new Date(iso).toTimeString().slice(0, 8));
    });

    it('preserves stored ts in text event', () => {
      const iso = '2026-05-06T09:22:45+00:00';
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          ts: iso,
          type: 'text',
          text_truncated: 'analyzing results...',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.kind).toBe('agent_text');
      expect(result!.ts).toBe(new Date(iso).toTimeString().slice(0, 8));
    });

    it('falls back to current time when ts is missing in phase_boundary', () => {
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          _phase_boundary: 'draft',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.kind).toBe('phase_boundary');
      expect(result!.ts).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('falls back to current time when ts is missing in tool_call', () => {
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          type: 'tool_call',
          tool_name: 'search',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.kind).toBe('tool_call');
      expect(result!.ts).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('falls back to current time when ts is missing in tool_result', () => {
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          type: 'tool_result',
          result_truncated: 'result data',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.kind).toBe('tool_result');
      expect(result!.ts).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('falls back to current time when ts is missing in text', () => {
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          type: 'text',
          text_truncated: 'some text',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.kind).toBe('agent_text');
      expect(result!.ts).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    });

    it('handles very old timestamps correctly', () => {
      const iso = '2026-01-01T00:00:00Z';
      const event: SseTranscriptEvent = {
        run_id: 'test-run',
        payload: {
          ts: iso,
          type: 'tool_call',
          tool_name: 'test',
        },
      };
      const result = transcriptToLogEvent(event);
      expect(result).not.toBeNull();
      expect(result!.ts).toBe(new Date(iso).toTimeString().slice(0, 8));
      // Should NOT equal current time
      const nowTime = new Date().toTimeString().slice(0, 8);
      expect(result!.ts).not.toBe(nowTime);
    });
  });

  // ── parseIso ────────────────────────────────────────────────────────────────

  describe('parseIso', () => {
    it('returns a Date for a valid UTC ISO string', () => {
      const iso = '2026-06-10T14:30:00Z';
      const d = parseIso(iso);
      expect(d).not.toBeNull();
      expect(d!.getTime()).toBe(new Date(iso).getTime());
    });

    it('returns a Date for a valid offset ISO string', () => {
      const iso = '2026-06-10T14:30:00+05:30';
      const d = parseIso(iso);
      expect(d).not.toBeNull();
      expect(d!.getTime()).toBe(new Date(iso).getTime());
    });

    it('returns null for null input', () => {
      expect(parseIso(null)).toBeNull();
    });

    it('returns null for undefined input', () => {
      expect(parseIso(undefined)).toBeNull();
    });

    it('returns null for an empty string', () => {
      expect(parseIso('')).toBeNull();
    });

    it('returns null for an unparseable string', () => {
      expect(parseIso('not-a-date')).toBeNull();
    });
  });

  // ── formatPhaseDuration ─────────────────────────────────────────────────────

  describe('formatPhaseDuration', () => {
    /**
     * Build a pair of ISO strings that are exactly `ms` milliseconds apart,
     * anchored at a fixed base so the test is timezone-independent.
     */
    function makeInterval(ms: number): [string, string] {
      const base = new Date('2026-06-10T10:00:00Z');
      const end = new Date(base.getTime() + ms);
      return [base.toISOString(), end.toISOString()];
    }

    it('formats a 2.5-second run as "2.5s"', () => {
      const [start, end] = makeInterval(2500);
      expect(formatPhaseDuration(start, end)).toBe('2.5s');
    });

    it('formats a 47-second run as "47.0s"', () => {
      const [start, end] = makeInterval(47000);
      expect(formatPhaseDuration(start, end)).toBe('47.0s');
    });

    it('formats exactly 60 seconds as "1m 0s"', () => {
      const [start, end] = makeInterval(60000);
      expect(formatPhaseDuration(start, end)).toBe('1m 0s');
    });

    it('formats a 3m 10s run correctly', () => {
      const [start, end] = makeInterval(190000); // 3*60 + 10 = 190s
      expect(formatPhaseDuration(start, end)).toBe('3m 10s');
    });

    it('formats a 12m 5s run correctly', () => {
      const [start, end] = makeInterval(725000); // 12*60 + 5 = 725s
      expect(formatPhaseDuration(start, end)).toBe('12m 5s');
    });

    it('returns "—" when startedAt is null', () => {
      const [, end] = makeInterval(5000);
      expect(formatPhaseDuration(null, end)).toBe('—');
    });

    it('returns "—" when finishedAt is null', () => {
      const [start] = makeInterval(5000);
      expect(formatPhaseDuration(start, null)).toBe('—');
    });

    it('returns "—" when both are null', () => {
      expect(formatPhaseDuration(null, null)).toBe('—');
    });

    it('returns "—" when startedAt is undefined', () => {
      const [, end] = makeInterval(5000);
      expect(formatPhaseDuration(undefined, end)).toBe('—');
    });

    it('returns "—" when finishedAt is undefined', () => {
      const [start] = makeInterval(5000);
      expect(formatPhaseDuration(start, undefined)).toBe('—');
    });

    it('returns "—" when startedAt is an invalid string', () => {
      const [, end] = makeInterval(5000);
      expect(formatPhaseDuration('not-a-date', end)).toBe('—');
    });

    it('returns "—" when finishedAt is an invalid string', () => {
      const [start] = makeInterval(5000);
      expect(formatPhaseDuration(start, 'not-a-date')).toBe('—');
    });

    it('returns "—" when end is before start (negative delta)', () => {
      const [start, end] = makeInterval(10000);
      // swap order to get a negative delta
      expect(formatPhaseDuration(end, start)).toBe('—');
    });

    it('result derives from fixture values, never from a hardcoded constant', () => {
      // Feed in timestamps with a known 1.4s delta — old code returned '1.4s'
      // as a hardcoded constant even for phase 1. Verify the value comes from
      // the math, not a constant, by using a 1.4s delta on all three phases.
      const [start, end] = makeInterval(1400);
      expect(formatPhaseDuration(start, end)).toBe('1.4s');

      // 3.8s — old phase-2 constant
      const [s2, e2] = makeInterval(3800);
      expect(formatPhaseDuration(s2, e2)).toBe('3.8s');

      // 12.1s — old phase-3 constant (now requires real timestamps)
      const [s3, e3] = makeInterval(12100);
      expect(formatPhaseDuration(s3, e3)).toBe('12.1s');
    });
  });
});
