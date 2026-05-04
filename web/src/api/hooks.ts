// React Query hooks over the jobsmith FastAPI backend.
//
// Slice 8 (SSE) will add `useEventStream(slug)` here — keep this file the
// single import point for live data so consumers stay mechanical.

import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { apiGet, apiGetText } from './client';
import type {
  Application,
  ApplicationDetail,
  MasterPayload,
} from './types';

// ── Query keys (export so consumers can invalidate from elsewhere) ───────

export const queryKeys = {
  applications: () => ['applications'] as const,
  application: (slug: string) => ['applications', slug] as const,
  master: () => ['master'] as const,
  raw: (slug: string, filename: string) =>
    ['applications', slug, 'raw', filename] as const,
};

// ── Hooks ────────────────────────────────────────────────────────────────

export function useApplications(): UseQueryResult<Application[]> {
  return useQuery<Application[]>({
    queryKey: queryKeys.applications(),
    queryFn: ({ signal }) => apiGet<Application[]>('/api/applications', signal),
  });
}

export function useApplication(
  slug: string | undefined,
): UseQueryResult<ApplicationDetail> {
  return useQuery<ApplicationDetail>({
    queryKey: slug ? queryKeys.application(slug) : ['applications', '__none__'],
    queryFn: ({ signal }) =>
      apiGet<ApplicationDetail>(`/api/applications/${slug}`, signal),
    enabled: Boolean(slug),
  });
}

export function useMaster(): UseQueryResult<MasterPayload> {
  return useQuery<MasterPayload>({
    queryKey: queryKeys.master(),
    queryFn: ({ signal }) => apiGet<MasterPayload>('/api/master', signal),
  });
}

/** Lazy fetch raw text artifact — exposed for "View full file" links. */
export function useRawArtifact(
  slug: string | undefined,
  filename: string | undefined,
): UseQueryResult<string> {
  return useQuery<string>({
    queryKey:
      slug && filename
        ? queryKeys.raw(slug, filename)
        : ['applications', '__none__', 'raw'],
    queryFn: ({ signal }) =>
      apiGetText(`/api/applications/${slug}/raw/${filename}`, signal),
    enabled: Boolean(slug && filename),
  });
}
