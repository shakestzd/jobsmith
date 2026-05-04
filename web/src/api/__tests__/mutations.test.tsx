// Mutation hook tests (slice 4 / feat-7784ef64).
//
// Coverage strategy: 4 representative cases —
//   1. useCreateApplication happy path → 201 returns slug + run_id.
//   2. useCreateApplication 400 surfaces ApiError with the detail body.
//   3. useRerunApplication happy path → 202 returns slug + run_id.
//   4. useRerunApplication 409 surfaces the conflict body so the UI can
//      show the in-flight run_id pointer.
//
// We do NOT test every combination of inputs; we test the wiring (request
// body shape, response handling, error surfacing) and trust React Query
// for everything else.

import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import {
  QueryClient,
  QueryClientProvider,
  type QueryClientConfig,
} from '@tanstack/react-query';
import { renderHook, waitFor, act } from '@testing-library/react';
import type { ReactNode } from 'react';
import React from 'react';

import { server } from '../../test-setup';
import { useCreateApplication, useRerunApplication } from '../hooks';
import { ApiError } from '../client';
import type {
  CreateApplicationRequest,
  CreateApplicationResponse,
  RerunRequest,
  RerunResponse,
  RerunConflictResponse,
} from '../types';

const BASE = 'http://localhost:8000';

function makeWrapper() {
  const config: QueryClientConfig = {
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  };
  const client = new QueryClient(config);
  function Wrapper({ children }: { children: ReactNode }) {
    return React.createElement(
      QueryClientProvider,
      { client },
      children,
    );
  }
  return Wrapper;
}

describe('useCreateApplication', () => {
  it('POSTs to /api/applications and returns slug + run_id on 201', async () => {
    const captured: { body?: CreateApplicationRequest } = {};
    const respBody: CreateApplicationResponse = {
      slug: 'linear-product-engineer-2026-05',
      run_id: 'run-abc123',
      events_url: '/api/applications/linear-product-engineer-2026-05/events',
    };
    server.use(
      http.post(`${BASE}/api/applications`, async ({ request }) => {
        captured.body = (await request.json()) as CreateApplicationRequest;
        return HttpResponse.json(respBody, { status: 201 });
      }),
    );

    const { result } = renderHook(() => useCreateApplication(), {
      wrapper: makeWrapper(),
    });

    const reqBody: CreateApplicationRequest = {
      jd_url: 'https://linear.app/careers/product-engineer',
      jd_text: null,
      jd_file_b64: null,
      verbosity: '-v',
      skip_confirmations: true,
      force: false,
    };

    await act(async () => {
      await result.current.mutateAsync(reqBody);
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.slug).toBe(respBody.slug);
    expect(result.current.data?.run_id).toBe(respBody.run_id);
    expect(captured.body?.jd_url).toBe(reqBody.jd_url);
    expect(captured.body?.verbosity).toBe('-v');
    expect(captured.body?.skip_confirmations).toBe(true);
  });

  it('surfaces 400 ApiError when the backend rejects the payload', async () => {
    server.use(
      http.post(`${BASE}/api/applications`, () =>
        HttpResponse.json({ detail: 'jd_url is invalid' }, { status: 400 }),
      ),
    );

    const { result } = renderHook(() => useCreateApplication(), {
      wrapper: makeWrapper(),
    });

    await act(async () => {
      try {
        await result.current.mutateAsync({
          jd_url: 'not-a-url',
          jd_text: null,
          jd_file_b64: null,
          verbosity: '-v',
          skip_confirmations: false,
          force: false,
        });
      } catch {
        // expected
      }
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    const err = result.current.error;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(400);
    expect((err as ApiError).body).toContain('jd_url is invalid');
  });
});

describe('useRerunApplication', () => {
  it('POSTs to /api/applications/{slug}/run and returns 202 success body', async () => {
    const slug = 'anthropic-applied-ai-2026-04';
    const captured: { body?: RerunRequest } = {};
    const respBody: RerunResponse = {
      slug,
      run_id: 'run-xyz789',
      events_url: `/api/applications/${slug}/events`,
    };
    server.use(
      http.post(`${BASE}/api/applications/${slug}/run`, async ({ request }) => {
        captured.body = (await request.json()) as RerunRequest;
        return HttpResponse.json(respBody, { status: 202 });
      }),
    );

    const { result } = renderHook(() => useRerunApplication(slug), {
      wrapper: makeWrapper(),
    });

    await act(async () => {
      await result.current.mutateAsync({ verbosity: '-v', force: false });
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.run_id).toBe(respBody.run_id);
    expect(captured.body?.verbosity).toBe('-v');
    expect(captured.body?.force).toBe(false);
  });

  it('surfaces 409 conflict body so the UI can show the running run_id', async () => {
    const slug = 'busy-application';
    const conflict: RerunConflictResponse = {
      slug,
      run_id: 'run-already-going',
      status: 'running',
      events_url: `/api/applications/${slug}/events`,
    };
    server.use(
      http.post(`${BASE}/api/applications/${slug}/run`, () =>
        HttpResponse.json(conflict, { status: 409 }),
      ),
    );

    const { result } = renderHook(() => useRerunApplication(slug), {
      wrapper: makeWrapper(),
    });

    await act(async () => {
      try {
        await result.current.mutateAsync({ verbosity: '-v', force: false });
      } catch {
        // expected
      }
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    const err = result.current.error;
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    // The conflict body must be parseable from err.body so callers can show
    // the in-flight run_id without auto-redirecting.
    const parsed = JSON.parse((err as ApiError).body) as RerunConflictResponse;
    expect(parsed.run_id).toBe('run-already-going');
    expect(parsed.status).toBe('running');
  });
});
