// Smoke tests for hooks.ts. We exercise each hook against a stubbed fetch
// and assert: (a) the right URL/method was called, (b) the returned data
// matches what the API stub yielded, (c) mutations invalidate the right
// cache keys.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import {
  QueryClient,
  QueryClientProvider,
  type QueryClient as QC,
} from '@tanstack/react-query';
import React from 'react';

import {
  queryKeys,
  useApplications,
  useApplication,
  useMaster,
  useUpdateMaster,
  useBenchmark,
  useUpdateBenchmark,
  useArtifact,
  usePutArtifact,
  useCreateApplication,
  useRerunApplication,
} from '../hooks';
import type { Application, ApplicationDetail, ArtifactEnvelope, BenchmarkResponse, MasterPayload } from '../types';

function makeWrapper(client: QC) {
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client }, children);
}

function freshClient(): QC {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function stubFetch(responder: (call: FetchCall) => Response): { calls: FetchCall[]; restore: () => void } {
  const calls: FetchCall[] = [];
  const original = global.fetch;
  global.fetch = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === 'string' ? input : (input as Request).url ?? String(input);
    const call: FetchCall = { url, init };
    calls.push(call);
    return responder(call);
  }) as typeof global.fetch;
  return {
    calls,
    restore: () => {
      global.fetch = original;
    },
  };
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...(init ?? {}),
  });
}

describe('queryKeys', () => {
  it('produces stable, narrow keys', () => {
    expect(queryKeys.applications()).toEqual(['applications']);
    expect(queryKeys.application('foo')).toEqual(['applications', 'foo']);
    expect(queryKeys.master()).toEqual(['master']);
    expect(queryKeys.masterSection('skill')).toEqual(['master', 'skill']);
    expect(queryKeys.benchmark()).toEqual(['master', 'benchmark']);
    expect(queryKeys.artifact('s', 'r', 'k')).toEqual(['artifact', 's', 'r', 'k']);
  });
});

describe('useApplications', () => {
  let stub: ReturnType<typeof stubFetch>;
  afterEach(() => stub.restore());

  it('GETs /api/applications and returns the list', async () => {
    const apps: Application[] = [
      { slug: 'foo', run_id: 'r1', phase: '1', status: 'running', started_at: null, finished_at: null },
    ];
    stub = stubFetch(() => jsonResponse(apps));

    const client = freshClient();
    const { result } = renderHook(() => useApplications(), { wrapper: makeWrapper(client) });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(apps);
    expect(stub.calls[0].url).toContain('/api/applications');
    expect(stub.calls[0].init.method).toBe('GET');
  });
});

describe('useApplication', () => {
  let stub: ReturnType<typeof stubFetch>;
  afterEach(() => stub.restore());

  it('skips the fetch when slug is empty', () => {
    stub = stubFetch(() => jsonResponse({}));
    const client = freshClient();
    renderHook(() => useApplication(''), { wrapper: makeWrapper(client) });
    expect(stub.calls.length).toBe(0);
  });

  it('GETs /api/applications/{slug} and returns detail', async () => {
    const detail: ApplicationDetail = {
      slug: 'foo',
      run_id: 'r1',
      phase: '1',
      status: 'running',
      started_at: null,
      finished_at: null,
      artifacts: [],
    };
    stub = stubFetch(() => jsonResponse(detail));
    const client = freshClient();
    const { result } = renderHook(() => useApplication('foo'), { wrapper: makeWrapper(client) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.slug).toBe('foo');
    expect(stub.calls[0].url).toContain('/api/applications/foo');
  });
});

describe('useMaster + useUpdateMaster', () => {
  let stub: ReturnType<typeof stubFetch>;
  afterEach(() => stub.restore());

  it('GETs /api/master', async () => {
    const payload: MasterPayload = { work: [], skill: [], education: [], author: null };
    stub = stubFetch(() => jsonResponse(payload));
    const client = freshClient();
    const { result } = renderHook(() => useMaster(), { wrapper: makeWrapper(client) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(stub.calls[0].url).toContain('/api/master');
  });

  it('PUTs the section and invalidates master queries on success', async () => {
    stub = stubFetch(() => jsonResponse({ section: 'skill', path: '/x', bytes_written: 0 }));
    const client = freshClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');

    const { result } = renderHook(() => useUpdateMaster<'skill'>(), { wrapper: makeWrapper(client) });

    await act(async () => {
      await result.current.mutateAsync({ section: 'skill', payload: [] });
    });

    const call = stub.calls[0];
    expect(call.url).toContain('/api/master/skill');
    expect(call.init.method).toBe('PUT');
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['master'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['master', 'skill'] });
  });
});

describe('useBenchmark + useUpdateBenchmark', () => {
  let stub: ReturnType<typeof stubFetch>;
  afterEach(() => stub.restore());

  it('GETs /api/master/benchmark', async () => {
    const benchmark: BenchmarkResponse = { text: '# hello', version: 'abc' };
    stub = stubFetch(() => jsonResponse(benchmark));
    const client = freshClient();
    const { result } = renderHook(() => useBenchmark(), { wrapper: makeWrapper(client) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.version).toBe('abc');
  });

  it('PUTs benchmark with If-Match when ifMatch is provided', async () => {
    stub = stubFetch(() => jsonResponse({ text: 'new', version: 'v2' }));
    const client = freshClient();
    const { result } = renderHook(() => useUpdateBenchmark(), { wrapper: makeWrapper(client) });

    await act(async () => {
      await result.current.mutateAsync({ text: 'new', ifMatch: 'v1' });
    });

    const call = stub.calls[0];
    const headers = (call.init.headers as Record<string, string>);
    expect(headers['If-Match']).toBe('"v1"');
  });

  it('PUTs benchmark without If-Match when omitted', async () => {
    stub = stubFetch(() => jsonResponse({ text: 'new', version: 'v2' }));
    const client = freshClient();
    const { result } = renderHook(() => useUpdateBenchmark(), { wrapper: makeWrapper(client) });

    await act(async () => {
      await result.current.mutateAsync({ text: 'new' });
    });

    const call = stub.calls[0];
    const headers = (call.init.headers as Record<string, string>);
    expect(headers['If-Match']).toBeUndefined();
  });
});

describe('useArtifact + usePutArtifact', () => {
  let stub: ReturnType<typeof stubFetch>;
  afterEach(() => stub.restore());

  it('skips fetching when any of slug/runId/kind is empty', () => {
    stub = stubFetch(() => jsonResponse({}));
    const client = freshClient();
    renderHook(() => useArtifact({ slug: '', runId: 'r', kind: 'k' }), {
      wrapper: makeWrapper(client),
    });
    expect(stub.calls.length).toBe(0);
  });

  it('PUTs artifact with If-Match and invalidates application+artifact', async () => {
    const env: ArtifactEnvelope = {
      run_id: 'r1', specialist: 's1', kind: 'jd-parsed', output: {},
      finished_at: null, transcript_ref: null, version: 2,
    };
    stub = stubFetch(() => jsonResponse(env));
    const client = freshClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { result } = renderHook(() => usePutArtifact(), { wrapper: makeWrapper(client) });

    await act(async () => {
      await result.current.mutateAsync({
        slug: 'foo', runId: 'r1', kind: 'jd-parsed',
        body: { output: { company: 'X' } },
        ifMatch: 1,
      });
    });

    const call = stub.calls[0];
    expect(call.url).toContain('/api/applications/foo/runs/r1/artifacts/jd-parsed');
    expect((call.init.headers as Record<string, string>)['If-Match']).toBe('"1"');
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['artifact', 'foo', 'r1', 'jd-parsed'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['applications', 'foo'] });
  });
});

describe('useCreateApplication + useRerunApplication', () => {
  let stub: ReturnType<typeof stubFetch>;
  afterEach(() => stub.restore());

  it('POSTs /api/applications and invalidates the list', async () => {
    stub = stubFetch(() => jsonResponse({ slug: 'new', run_id: 'r9', events_url: '/events' }));
    const client = freshClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { result } = renderHook(() => useCreateApplication(), { wrapper: makeWrapper(client) });

    await act(async () => {
      await result.current.mutateAsync({ jd_url: 'https://example.com/job' });
    });

    const call = stub.calls[0];
    expect(call.init.method).toBe('POST');
    expect(call.url.endsWith('/api/applications')).toBe(true);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['applications'] });
  });

  it('POSTs /api/applications/{slug}/run and invalidates list + detail', async () => {
    stub = stubFetch(() => jsonResponse({ slug: 'foo', run_id: 'r9', events_url: '/events' }));
    const client = freshClient();
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { result } = renderHook(() => useRerunApplication(), { wrapper: makeWrapper(client) });

    await act(async () => {
      await result.current.mutateAsync({ slug: 'foo' });
    });

    const call = stub.calls[0];
    expect(call.url.endsWith('/api/applications/foo/run')).toBe(true);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['applications'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['applications', 'foo'] });
  });
});
