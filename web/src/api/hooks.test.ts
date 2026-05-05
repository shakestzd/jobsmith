// hooks.test.ts — regression for POST→GET 404 race (feat-092c5a2c, GH#60).
//
// After POST /api/applications returns 201, the server may take ~1-2s before
// GET /api/applications/{slug} returns 200. Without a retry, `useApplication`
// immediately exposes a 404 error, which renders an error card in the UI
// instead of the running-application detail view.
//
// Fix: `fetchApplicationWithRetry` retries on 404 up to 5 times with linear
// backoff before surfacing the error. `useApplication` delegates to it.
// Tests use delayMs=0 to keep the suite fast.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { fetchApplicationWithRetry, useApplication } from './hooks';

// Mock the client module so we control what apiGet returns.
vi.mock('./client', () => ({
  apiGet: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = 'JobsmithApiError';
      this.status = status;
    }
  },
}));

import { apiGet, JobsmithApiError } from './client';

type JsaError = InstanceType<typeof JobsmithApiError>;

function make404(): JsaError {
  return new (JobsmithApiError as unknown as { new(m: string, s: number): JsaError })('Not Found', 404);
}

function make401(): JsaError {
  return new (JobsmithApiError as unknown as { new(m: string, s: number): JsaError })('Unauthorized', 401);
}

const DETAIL_FIXTURE = {
  slug: 'linear-engineer-2026-05',
  run_id: 'run-race-1',
  phase: 'gather',
  status: 'running',
  ui_phase: 'running',
  started_at: '2026-05-05T10:00:00Z',
  finished_at: null,
  role: 'Product Engineer',
  company: 'Linear',
  artifacts: [],
  url: 'https://linear.app/careers/product-engineer',
};

// Signal helper — simulates the cancelled flag used in hooks.
function liveSignal(): { cancelled: boolean } {
  return { cancelled: false };
}

describe('fetchApplicationWithRetry — POST→GET 404 race (feat-092c5a2c)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('FAILING TODAY: resolves data on second call when first GET returns 404', async () => {
    // Simulate the race: first call returns 404, second returns 200.
    let callCount = 0;
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) return Promise.reject(make404());
      return Promise.resolve(DETAIL_FIXTURE);
    });

    // delayMs=0 so retries are instant in tests.
    const result = await fetchApplicationWithRetry(
      'linear-engineer-2026-05',
      liveSignal(),
      0,
    );

    // Must have data — NOT an error — after the 404 was retried.
    expect(result).toEqual(DETAIL_FIXTURE);
    // apiGet was called exactly twice (first 404, then 200).
    expect(apiGet).toHaveBeenCalledTimes(2);
  });

  it('FAILING TODAY: resolves after 2 × 404 then 200 (simulates ~1-2s server lag)', async () => {
    let callCount = 0;
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation(() => {
      callCount += 1;
      if (callCount <= 2) return Promise.reject(make404());
      return Promise.resolve(DETAIL_FIXTURE);
    });

    const result = await fetchApplicationWithRetry(
      'linear-engineer-2026-05',
      liveSignal(),
      0,
    );

    expect(result).toEqual(DETAIL_FIXTURE);
    expect(apiGet).toHaveBeenCalledTimes(3);
  });

  it('surfaces error immediately (no retry) on non-404 errors like 401', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockRejectedValue(make401());

    await expect(
      fetchApplicationWithRetry('some-slug', liveSignal(), 0),
    ).rejects.toMatchObject({ status: 401 });

    // Only 1 call — 401 is not retried.
    expect(apiGet).toHaveBeenCalledTimes(1);
  });

  it('surfaces error after exhausting all 5 retries when GET keeps returning 404', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockRejectedValue(make404());

    await expect(
      fetchApplicationWithRetry('always-404-slug', liveSignal(), 0),
    ).rejects.toMatchObject({ status: 404 });

    // 1 initial + 5 retries = 6 calls total.
    expect(apiGet).toHaveBeenCalledTimes(6);
  });
});

describe('useApplication hook — delegates to retry logic (feat-092c5a2c)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('FAILING TODAY: hook resolves data after 404-then-200 race', async () => {
    let callCount = 0;
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) return Promise.reject(make404());
      return Promise.resolve(DETAIL_FIXTURE);
    });

    const { result } = renderHook(() =>
      useApplication('linear-engineer-2026-05'),
    );

    // Initially loading.
    expect(result.current.isLoading).toBe(true);

    // Wait for the retry to resolve. The default delay (200ms) means we need
    // to wait a bit longer than 200ms for the first retry to fire.
    await waitFor(
      () => {
        expect(result.current.isLoading).toBe(false);
      },
      { timeout: 2000 },
    );

    // Must have data — NOT an error.
    expect(result.current.error).toBeNull();
    expect(result.current.data).toEqual(DETAIL_FIXTURE);
  });

  it('hook surfaces error for non-404 errors without retrying', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockRejectedValue(make401());

    const { result } = renderHook(() => useApplication('some-slug'));

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false);
    });

    expect(result.current.error).not.toBeNull();
    expect(result.current.data).toBeUndefined();
    // Only 1 call — no retry for 401.
    expect(apiGet).toHaveBeenCalledTimes(1);
  });
});

describe('useApplication hook — resets state on slug change (roborev job 947)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('clears stale data immediately when slug changes', async () => {
    const detailA = { ...DETAIL_FIXTURE, slug: 'slug-a', url: 'https://a/' };
    const detailB = { ...DETAIL_FIXTURE, slug: 'slug-b', url: 'https://b/' };

    type ResolveFn = (value: typeof detailB) => void;
    const pendingRef: { current: ResolveFn | null } = { current: null };
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
      if (path.endsWith('/slug-a')) return Promise.resolve(detailA);
      // slug-b: hold the promise so we can observe the in-flight state.
      return new Promise<typeof detailB>((resolve) => {
        pendingRef.current = resolve;
      });
    });

    const { result, rerender } = renderHook(
      ({ slug }: { slug: string }) => useApplication(slug),
      { initialProps: { slug: 'slug-a' } },
    );

    await waitFor(() => expect(result.current.data).toEqual(detailA));

    rerender({ slug: 'slug-b' });

    // Immediately after the slug changes, stale data must be gone and the
    // hook must report loading. The pending fetch for slug-b has not resolved.
    expect(result.current.data).toBeUndefined();
    expect(result.current.isLoading).toBe(true);
    expect(result.current.error).toBeNull();

    // Resolve the pending fetch and confirm the new slug's data lands.
    pendingRef.current?.(detailB);
    await waitFor(() => expect(result.current.data).toEqual(detailB));
  });

  it('clears prior error when slug changes', async () => {
    const detailB = { ...DETAIL_FIXTURE, slug: 'slug-b' };
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
      if (path.endsWith('/slug-a')) return Promise.reject(make401());
      return Promise.resolve(detailB);
    });

    const { result, rerender } = renderHook(
      ({ slug }: { slug: string }) => useApplication(slug),
      { initialProps: { slug: 'slug-a' } },
    );

    await waitFor(() => expect(result.current.error).not.toBeNull());

    rerender({ slug: 'slug-b' });
    // Error must be cleared on slug change, not held over from the prior slug.
    expect(result.current.error).toBeNull();

    await waitFor(() => expect(result.current.data).toEqual(detailB));
  });

  it('clears data when slug becomes empty', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(DETAIL_FIXTURE);

    const { result, rerender } = renderHook(
      ({ slug }: { slug: string }) => useApplication(slug),
      { initialProps: { slug: 'linear-engineer-2026-05' } },
    );

    await waitFor(() => expect(result.current.data).toEqual(DETAIL_FIXTURE));

    rerender({ slug: '' });
    expect(result.current.data).toBeUndefined();
    expect(result.current.error).toBeNull();
    expect(result.current.isLoading).toBe(false);
  });
});
