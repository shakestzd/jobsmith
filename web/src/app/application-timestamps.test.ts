// application-timestamps.test.ts — unit tests for timestamp handling in event stream
//
// Assertions:
//   formatEventTime() converts ISO timestamps to HH:MM:SS or falls back to now()
//   pickPayloadTime() extracts ts from payload or returns now()
//   transcriptToLogEvent() uses stored timestamp instead of receipt time
//   Stored timestamps far in the past are preserved in LogEvent
//   Missing ts field falls back gracefully

import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  formatEventTime,
  pickPayloadTime,
  transcriptToLogEvent,
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
});
