// Tests for the SSE parser. We focus on parseSseChunk because it is the
// only pure function in events.ts; openEventStream wraps fetch + ReadableStream
// which is exercised end-to-end via the integration tests in hooks.test.tsx
// (useEventStream).

import { describe, it, expect } from 'vitest';
import { parseSseChunk } from '../events';

describe('parseSseChunk', () => {
  it('returns no frames on empty input', () => {
    expect(parseSseChunk('')).toEqual([]);
  });

  it('parses a single complete frame', () => {
    const chunk = 'event: phase\ndata: {"a":1}\n\n';
    expect(parseSseChunk(chunk)).toEqual([
      { event: 'phase', data: '{"a":1}' },
    ]);
  });

  it('parses multiple frames in one chunk', () => {
    const chunk =
      'event: phase\ndata: {"a":1}\n\n' +
      'event: specialist\ndata: {"b":2}\n\n';
    expect(parseSseChunk(chunk)).toEqual([
      { event: 'phase', data: '{"a":1}' },
      { event: 'specialist', data: '{"b":2}' },
    ]);
  });

  it('ignores comment / heartbeat lines', () => {
    const chunk = ': ping\nevent: phase\ndata: {"a":1}\n\n';
    expect(parseSseChunk(chunk)).toEqual([
      { event: 'phase', data: '{"a":1}' },
    ]);
  });

  it('joins multi-line data fields with newlines', () => {
    const chunk = 'event: log\ndata: line one\ndata: line two\n\n';
    expect(parseSseChunk(chunk)).toEqual([
      { event: 'log', data: 'line one\nline two' },
    ]);
  });

  it('produces a frame with null event when only data is present', () => {
    const chunk = 'data: {"x":1}\n\n';
    expect(parseSseChunk(chunk)).toEqual([
      { event: null, data: '{"x":1}' },
    ]);
  });

  it('drops a frame when no data line is present', () => {
    const chunk = 'event: phase\n\n';
    expect(parseSseChunk(chunk)).toEqual([]);
  });

  it('treats `data: <json>\\n\\n` as a frame with null event (per SSE spec)', () => {
    const chunk = 'data: {"a":1}\n\n';
    const frames = parseSseChunk(chunk);
    expect(frames).toEqual([{ event: null, data: '{"a":1}' }]);
    // The frame is preserved by the parser; dispatch in events.ts surfaces
    // unknown event types (including the spec-default "message") via
    // onError rather than silently dropping the frame.
  });
});
