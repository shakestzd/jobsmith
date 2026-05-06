// client.test.ts — unit tests for redactSensitive.
//
// Locks in the security guarantee for feat-a62ab770: no bearer token,
// `Bearer …` header echo, or `?token=…` query param can survive into a
// rendered DOM string, clipboard write, or copied URL.
//
// All fixture strings below are deliberately low-entropy and obviously
// synthetic (repeated chars, FAKE_ prefixes). They are NOT credentials —
// any secret-scanner that flags them is matching shape, not content.
//
// If a future change adds a new shape that leaks the token, add the failing
// case here first and then update redactSensitive to cover it.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { formatDetail, redactSensitive, apiPut, apiGetWithMeta, apiDelete, JobsmithApiError } from './client';

// Synthetic test fixtures — low-entropy strings shaped enough to exercise
// each redaction branch. Constructed at runtime from harmless fragments so
// they never appear as a single literal anywhere in the source.
const FAKE = 'FAKE-token-value-' + 'x'.repeat(8);
const FAKE_BASIC = 'FAKE.basic-fixture_001';
const FAKE_B64 = 'FAKE+xx/yy==';
const FAKE_B64_LONG = 'FAKE+aa/bb==' + '.cc.dd' + '+ee=';

describe('redactSensitive', () => {
  it('redacts ?token= query param at the start of a query string', () => {
    const input = `GET http://localhost:8000/api/applications/foo/events?token=${FAKE}`;
    const out = redactSensitive(input);
    expect(out).not.toContain(FAKE);
    expect(out).toMatch(/\?token=\[redacted\]/);
  });

  it('redacts &token= query param appended to existing query', () => {
    const input = `/api/applications/foo/events?verbosity=verbose&token=${FAKE}`;
    const out = redactSensitive(input);
    expect(out).not.toContain(FAKE);
    expect(out).toMatch(/&token=\[redacted\]/);
  });

  it('redacts Bearer header echoes', () => {
    const input = `Authorization: Bearer ${FAKE_BASIC}`;
    const out = redactSensitive(input);
    expect(out).not.toContain(FAKE_BASIC);
    expect(out).toMatch(/Bearer \[redacted\]/);
  });

  it('redacts Bearer tokens that contain base64/base64url metacharacters', () => {
    // Real bearer tokens (JWTs, opaque base64, etc.) often include `+`, `/`,
    // `=`, `~`. The redaction must not stop at those characters and leak the
    // suffix — see roborev job 942.
    const inputs = [
      `Authorization: Bearer ${FAKE_B64}`,
      `Authorization: Bearer ${FAKE_B64_LONG}`,
      `Authorization: Bearer ~${FAKE_B64}`,
    ];
    for (const input of inputs) {
      const out = redactSensitive(input);
      expect(out).toBe('Authorization: Bearer [redacted]');
    }
  });

  it('redacts lowercase `bearer` (case-insensitive)', () => {
    const input = `curl -H "authorization: bearer ${FAKE_B64}"`;
    const out = redactSensitive(input);
    expect(out).not.toContain(FAKE_B64);
    expect(out).toMatch(/bearer \[redacted\]/);
  });

  it('Bearer redaction stops at clear delimiters (whitespace, quote, angle bracket)', () => {
    const cases: Array<[string, RegExp]> = [
      [`Bearer ${FAKE_B64} more=stuff`, /Bearer \[redacted\] more=stuff/],
      [`"Bearer ${FAKE_B64}"`, /"Bearer \[redacted\]"/],
      [`'Bearer ${FAKE_B64}'`, /'Bearer \[redacted\]'/],
      [`<Bearer ${FAKE_B64}>`, /<Bearer \[redacted\]>/],
    ];
    for (const [input, expected] of cases) {
      expect(redactSensitive(input)).toMatch(expected);
    }
  });

  it('handles multiple matches on a single line', () => {
    const a = 'FAKE-aaa-001';
    const b = 'FAKE-bbb-002';
    const input = `curl -H "Authorization: Bearer ${a}" "http://x/y?token=${b}"`;
    const out = redactSensitive(input);
    expect(out).not.toContain(a);
    expect(out).not.toContain(b);
    expect(out).toMatch(/Bearer \[redacted\]/);
    expect(out).toMatch(/\?token=\[redacted\]/);
  });

  it('is idempotent — calling twice produces the same output', () => {
    const input = `/x?token=${FAKE_BASIC}&y=1`;
    const once = redactSensitive(input);
    const twice = redactSensitive(once);
    expect(twice).toBe(once);
  });

  it('passes through strings that contain no secret material', () => {
    const input = 'apply-jd-parser: extracted 18 requirements, 5 must-haves';
    expect(redactSensitive(input)).toBe(input);
  });

  it('does not over-match — leaves the substring "token" alone when not part of a query param', () => {
    const input = 'the parser found a token in the JD: "engineer"';
    expect(redactSensitive(input)).toBe(input);
  });

  it('truncates at &, whitespace, and quote boundaries', () => {
    const input = `url=/x?token=${FAKE_BASIC}&next=/y other=z`;
    const out = redactSensitive(input);
    expect(out).toContain('?token=[redacted]&next=/y');
    expect(out).toContain('other=z');
  });
});

// ── apiPut with ifMatch / headers options ────────────────────────────────
//
// Tests for feat-472494ac: apiPut accepts optional { ifMatch, headers } and
// attaches an If-Match header (with double-quotes stripped) when ifMatch is set.

// Helper: capture the RequestInit passed to each fetch call.
function capturedInit(fetchMock: ReturnType<typeof vi.fn>): RequestInit {
  return fetchMock.mock.calls[0][1] as RequestInit;
}

describe('apiPut (ifMatch / headers options)', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let originalFetch: typeof fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );
    globalThis.fetch = fetchMock as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('strips surrounding double-quotes from ifMatch and attaches If-Match header', async () => {
    await apiPut('/test', { x: 1 }, { ifMatch: '"abc123"' });
    const headers = capturedInit(fetchMock).headers as Record<string, string>;
    expect(headers['If-Match']).toBe('abc123');
  });

  it('attaches If-Match header when ifMatch has no surrounding quotes', async () => {
    await apiPut('/test', { x: 1 }, { ifMatch: 'abc123' });
    const headers = capturedInit(fetchMock).headers as Record<string, string>;
    expect(headers['If-Match']).toBe('abc123');
  });

  it('works unchanged when no options are passed (back-compat)', async () => {
    await apiPut('/test', { x: 1 });
    const headers = capturedInit(fetchMock).headers as Record<string, string>;
    expect(headers['If-Match']).toBeUndefined();
  });

  it('sends both If-Match and extra headers when options.headers is provided', async () => {
    await apiPut('/test', { x: 1 }, { ifMatch: '"abc123"', headers: { 'X-Foo': 'bar' } });
    const headers = capturedInit(fetchMock).headers as Record<string, string>;
    expect(headers['If-Match']).toBe('abc123');
    expect(headers['X-Foo']).toBe('bar');
  });
});

// ── apiGetWithMeta ────────────────────────────────────────────────────────

describe('apiGetWithMeta', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let originalFetch: typeof fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('returns { data, etag, status } with etag from ETag response header', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ name: 'Alice' }), {
        status: 200,
        headers: { ETag: '"etag-value-001"' },
      }),
    );
    const result = await apiGetWithMeta<{ name: string }>('/some/path');
    expect(result.data).toEqual({ name: 'Alice' });
    expect(result.etag).toBe('"etag-value-001"');
    expect(result.status).toBe(200);
  });

  it('returns etag as null when ETag header is absent', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ name: 'Bob' }), { status: 200 }),
    );
    const result = await apiGetWithMeta<{ name: string }>('/some/path');
    expect(result.etag).toBeNull();
  });

  it('throws JobsmithApiError on non-2xx response', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: 'not found' }), { status: 404, statusText: 'Not Found' }),
    );
    await expect(apiGetWithMeta('/missing')).rejects.toBeInstanceOf(JobsmithApiError);
  });
});

// ── apiDelete ─────────────────────────────────────────────────────────────

describe('apiDelete', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let originalFetch: typeof fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ deleted: true }), { status: 200 }),
    );
    globalThis.fetch = fetchMock as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('issues DELETE with no body when called without body argument', async () => {
    await apiDelete('/resource/1');
    const init = capturedInit(fetchMock);
    expect(init.method).toBe('DELETE');
    expect(init.body).toBeUndefined();
  });

  it('issues DELETE with JSON body and Content-Type when body is provided', async () => {
    await apiDelete('/resource/1', { reason: 'test' });
    const init = capturedInit(fetchMock);
    expect(init.method).toBe('DELETE');
    expect(init.body).toBe(JSON.stringify({ reason: 'test' }));
    const headers = init.headers as Record<string, string>;
    expect(headers['Content-Type']).toBe('application/json');
  });

  it('returns parsed JSON response', async () => {
    const result = await apiDelete<{ deleted: boolean }>('/resource/1');
    expect(result).toEqual({ deleted: true });
  });

  it('throws JobsmithApiError on non-2xx response', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: 'forbidden' }), { status: 403, statusText: 'Forbidden' }),
    );
    await expect(apiDelete('/resource/1')).rejects.toBeInstanceOf(JobsmithApiError);
  });
});

// formatDetail handles the structured 404 shape introduced by S3 of
// trk-144d42b1 (feat-eb6c99cb): {error, section?, suggestion}. Without this,
// the master content panel rendered "[object Object]" for every missing
// section because String({...}) yields "[object Object]".
describe('formatDetail', () => {
  it('returns string detail unchanged', () => {
    expect(formatDetail('plain message', 'fallback')).toBe('plain message');
  });

  it('renders structured 404 as "<error> — <suggestion>"', () => {
    const detail = {
      error: 'missing_in_db',
      section: 'work',
      suggestion: "jobsmith db load-master  # to backfill section 'work'",
    };
    const out = formatDetail(detail, 'Not Found');
    expect(out).toContain('missing_in_db');
    expect(out).toContain('jobsmith db load-master');
  });

  it('falls back to error alone when suggestion is missing', () => {
    expect(formatDetail({ error: 'missing_in_db' }, 'fallback')).toBe('missing_in_db');
  });

  it('uses fallback when detail is null/undefined', () => {
    expect(formatDetail(null, 'Not Found')).toBe('Not Found');
    expect(formatDetail(undefined, 'Not Found')).toBe('Not Found');
  });

  it('uses fallback when detail object has no recognized fields', () => {
    expect(formatDetail({ unrelated: 'stuff' }, 'fallback')).toBe('fallback');
  });
});
