// postings.test.ts — unit tests for postings API client functions.
//
// Coverage:
//  - getPostings: builds correct URL with no filter
//  - getPostings: builds correct query string for each filter
//  - setPostingStatus: POSTs to correct path with body
//  - promotePosting: POSTs to correct path

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock fetch globally so no real network calls are made
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

// Mock window.localStorage and window.__JOBSMITH__ for token resolution
vi.stubGlobal('localStorage', {
  getItem: vi.fn(() => null),
  setItem: vi.fn(),
  removeItem: vi.fn(),
});

// Import after stubs are in place
import { getPostings, setPostingStatus, promotePosting } from './client';

function makeOkResponse(data: unknown) {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(data),
    headers: { get: () => null },
  };
}

describe('getPostings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValue(makeOkResponse([]));
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('calls GET /api/postings with no filter', async () => {
    await getPostings();
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toMatch(/\/api\/postings$/);
  });

  it('appends status filter', async () => {
    await getPostings({ status: 'sourced' });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('status=sourced');
  });

  it('appends source filter', async () => {
    await getPostings({ source: 'greenhouse' });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('source=greenhouse');
  });

  it('appends specialty filter', async () => {
    await getPostings({ specialty: 'backend' });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('specialty=backend');
  });

  it('appends min_score filter', async () => {
    await getPostings({ min_score: 0.7 });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('min_score=0.7');
  });

  it('combines multiple filters', async () => {
    await getPostings({ status: 'sourced', source: 'greenhouse', min_score: 0.5 });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain('status=sourced');
    expect(url).toContain('source=greenhouse');
    expect(url).toContain('min_score=0.5');
  });
});

describe('setPostingStatus', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValue(makeOkResponse({ id: 1, status: 'dismissed' }));
  });

  it('POSTs to /api/postings/{id}/status with body', async () => {
    await setPostingStatus(42, 'dismissed');
    const url = mockFetch.mock.calls[0][0] as string;
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect(url).toMatch(/\/api\/postings\/42\/status$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ status: 'dismissed' });
  });
});

describe('promotePosting', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockResolvedValue(
      makeOkResponse({ run_id: 'run-abc', slug: 'stripe-senior-eng', jd_fetch_failed: false })
    );
  });

  it('POSTs to /api/postings/{id}/promote with empty body', async () => {
    const result = await promotePosting(99);
    const url = mockFetch.mock.calls[0][0] as string;
    const init = mockFetch.mock.calls[0][1] as RequestInit;
    expect(url).toMatch(/\/api\/postings\/99\/promote$/);
    expect(init.method).toBe('POST');
    expect(result.run_id).toBe('run-abc');
    expect(result.jd_fetch_failed).toBe(false);
  });
});
