// React Query hooks over the jobsmith FastAPI backend.
//
// Slice 8 (SSE) will add `useEventStream(slug)` here — keep this file the
// single import point for live data so consumers stay mechanical.

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import {
  apiGet,
  apiGetText,
  apiPost,
  apiPut,
  apiUploadFile,
  ApiError,
} from './client';
import type {
  Application,
  ApplicationDetail,
  Author,
  CreateApplicationRequest,
  CreateApplicationResponse,
  EducationEntry,
  MasterPayload,
  RerunRequest,
  RerunResponse,
  SkillEntry,
  WorkEntry,
} from './types';

// ── Master section types ─────────────────────────────────────────────────

export type MasterSection = 'work' | 'skill' | 'education' | 'author';

export type MasterSectionPayload<S extends MasterSection> =
  S extends 'work' ? WorkEntry[]
    : S extends 'skill' ? SkillEntry[]
    : S extends 'education' ? EducationEntry[]
    : S extends 'author' ? Author | { author: Author[] }
    : never;

export interface MasterWriteResponse {
  section: MasterSection;
  path: string;
  bytes_written: number;
}

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

// ── Mutations (slice 4 / feat-7784ef64) ──────────────────────────────────
//
// `useCreateApplication` and `useRerunApplication` are intentionally
// mechanical: they POST, surface ApiError on non-2xx, and invalidate the
// relevant queryKeys on success. UI concerns (navigation, modal close,
// 409-conflict messaging) live in the consumer — these hooks stay
// router-agnostic so they're trivial to test against msw.

export function useCreateApplication(): UseMutationResult<
  CreateApplicationResponse,
  ApiError,
  CreateApplicationRequest
> {
  const queryClient = useQueryClient();
  return useMutation<
    CreateApplicationResponse,
    ApiError,
    CreateApplicationRequest
  >({
    mutationFn: (body) =>
      apiPost<CreateApplicationRequest, CreateApplicationResponse>(
        '/api/applications',
        body,
      ),
    onSuccess: (resp) => {
      // Refresh the dashboard list and seed the detail cache key so the
      // navigation target has a queryKey ready for SSE consumers.
      queryClient.invalidateQueries({ queryKey: queryKeys.applications() });
      queryClient.invalidateQueries({ queryKey: queryKeys.application(resp.slug) });
    },
  });
}

export function useRerunApplication(
  slug: string,
): UseMutationResult<RerunResponse, ApiError, RerunRequest> {
  const queryClient = useQueryClient();
  return useMutation<RerunResponse, ApiError, RerunRequest>({
    mutationFn: (body) =>
      apiPost<RerunRequest, RerunResponse>(
        `/api/applications/${slug}/run`,
        body,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.application(slug) });
      queryClient.invalidateQueries({ queryKey: queryKeys.applications() });
    },
  });
}

// ── Master content mutations (MVP — feat-fbc2297e) ───────────────────────
//
// PUT  /api/master/{section}              replace section with validated body
// POST /api/master/{section}/upload       upload a raw .yml file
//
// Comments are NOT preserved across the parse/dump round-trip. The 0.8
// DB-as-source-of-truth track replaces this surface with ruamel.yaml-backed
// editing or DB-canonical state — see plan-{TBD}.

export function useUpdateMaster<S extends MasterSection>(
  section: S,
): UseMutationResult<MasterWriteResponse, ApiError, MasterSectionPayload<S>> {
  const queryClient = useQueryClient();
  return useMutation<MasterWriteResponse, ApiError, MasterSectionPayload<S>>({
    mutationFn: (body) =>
      apiPut<MasterSectionPayload<S>, MasterWriteResponse>(
        `/api/master/${section}`,
        body,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.master() });
    },
  });
}

export function useUploadMaster(
  section: MasterSection,
): UseMutationResult<MasterWriteResponse, ApiError, File> {
  const queryClient = useQueryClient();
  return useMutation<MasterWriteResponse, ApiError, File>({
    mutationFn: (file) =>
      apiUploadFile<MasterWriteResponse>(
        `/api/master/${section}/upload`,
        file,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.master() });
    },
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
