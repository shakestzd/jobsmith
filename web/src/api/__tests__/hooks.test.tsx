// Hook tests for the API layer. Uses msw to mock the FastAPI backend.
//
// Coverage strategy: 3 representative cases — happy path list, slug-gated
// detail (enabled flag), error propagation. We intentionally do NOT test
// every hook against every state; that's React Query's responsibility.

import { describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import {
  QueryClient,
  QueryClientProvider,
  type QueryClientConfig,
} from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import React from 'react';

import { server } from '../../test-setup';
import { useApplication, useApplications, useMaster } from '../hooks';
import type { Application, ApplicationDetail, MasterPayload } from '../types';

const BASE = 'http://localhost:8000';

function makeWrapper() {
  const config: QueryClientConfig = {
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
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

const SAMPLE_APP: Application = {
  slug: 'anthropic-applied-ai-2026-04',
  role: 'Member of Technical Staff, Applied AI',
  company: 'Anthropic',
  status: 'rendered',
  updated: '2026-04-30T12:00:00Z',
  phase: 3,
  anchors: '14/14',
  factcheck: 'pass',
  renders: ['resume.pdf', 'cover.pdf'],
  url: '/applications/anthropic-applied-ai-2026-04/',
};

describe('useApplications', () => {
  it('returns the list from /api/applications', async () => {
    server.use(
      http.get(`${BASE}/api/applications`, () =>
        HttpResponse.json([SAMPLE_APP]),
      ),
    );

    const { result } = renderHook(() => useApplications(), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toHaveLength(1);
    expect(result.current.data?.[0]?.slug).toBe(SAMPLE_APP.slug);
  });

  it('surfaces server errors via isError', async () => {
    server.use(
      http.get(`${BASE}/api/applications`, () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    );

    const { result } = renderHook(() => useApplications(), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeDefined();
  });
});

describe('useApplication', () => {
  it('is disabled when slug is undefined and fires when slug is provided', async () => {
    const detail: ApplicationDetail = {
      ...SAMPLE_APP,
      artifacts: { apply_state: [], rendered: [] },
      spec: null,
      prose_draft: null,
      cover_letter_draft: null,
      fact_check: null,
      anchor_check: null,
      bullet_selection: null,
      variables: null,
      config: null,
      truncated: false,
    };
    server.use(
      http.get(`${BASE}/api/applications/${SAMPLE_APP.slug}`, () =>
        HttpResponse.json(detail),
      ),
    );

    const { result, rerender } = renderHook(
      ({ slug }: { slug: string | undefined }) => useApplication(slug),
      {
        wrapper: makeWrapper(),
        initialProps: { slug: undefined as string | undefined },
      },
    );

    // Disabled query: not fetching, not successful yet
    expect(result.current.fetchStatus).toBe('idle');
    expect(result.current.isSuccess).toBe(false);

    rerender({ slug: SAMPLE_APP.slug });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.slug).toBe(SAMPLE_APP.slug);
    expect(result.current.data?.artifacts.apply_state).toEqual([]);
  });
});

describe('useMaster', () => {
  it('returns the master payload', async () => {
    const payload: MasterPayload = {
      work: [
        {
          title: 'Senior Engineer',
          location: 'Recurly',
          date: '2022 — present',
          description: 'Remote',
          details: ['Built things.', { bullet: 'Shipped more.', anchor: true }],
        },
      ],
      skill: [],
      education: [],
      author: null,
    };
    server.use(
      http.get(`${BASE}/api/master`, () => HttpResponse.json(payload)),
    );

    const { result } = renderHook(() => useMaster(), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.work).toHaveLength(1);
    expect(result.current.data?.work[0]?.title).toBe('Senior Engineer');
  });
});
