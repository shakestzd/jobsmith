// React Query hooks against PR #29's FastAPI surface.
//
// Decision: snake_case kept verbatim (no camelCase transform). Cache keys are
// stable arrays (e.g. ['applications'], ['master', section]) so manual
// invalidation in mutations stays trivial.

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';

import {
  apiGet,
  apiPost,
  apiPostMultipart,
  apiPut,
} from './client';
import { openEventStream, type SseEvent } from './events';
import type {
  Application,
  ApplicationDetail,
  ArtifactEnvelope,
  BenchmarkPayload,
  BenchmarkResponse,
  MasterPayload,
  MasterSection,
  MasterSectionPayload,
  MasterWriteResponse,
  PutArtifactBody,
  SnapshotRequest,
  SnapshotResponse,
  Verbosity,
} from './types';

// ── Cache key namespace ──────────────────────────────────────────────────

export const queryKeys = {
  applications: () => ['applications'] as const,
  application: (slug: string) => ['applications', slug] as const,
  master: () => ['master'] as const,
  masterSection: (section: MasterSection) => ['master', section] as const,
  benchmark: () => ['master', 'benchmark'] as const,
  artifact: (slug: string, runId: string, kind: string) =>
    ['artifact', slug, runId, kind] as const,
};

// ── Applications ─────────────────────────────────────────────────────────

export function useApplications(): UseQueryResult<Application[]> {
  return useQuery({
    queryKey: queryKeys.applications(),
    queryFn: ({ signal }) => apiGet<Application[]>('/api/applications', signal),
  });
}

export function useApplication(slug: string): UseQueryResult<ApplicationDetail> {
  return useQuery({
    queryKey: queryKeys.application(slug),
    queryFn: ({ signal }) =>
      apiGet<ApplicationDetail>(`/api/applications/${encodeURIComponent(slug)}`, signal),
    enabled: slug.length > 0,
  });
}

// ── Master content ───────────────────────────────────────────────────────

export function useMaster(): UseQueryResult<MasterPayload> {
  return useQuery({
    queryKey: queryKeys.master(),
    queryFn: ({ signal }) => apiGet<MasterPayload>('/api/master', signal),
  });
}

interface UpdateMasterArgs<S extends MasterSection> {
  section: S;
  payload: MasterSectionPayload<S>;
}

export function useUpdateMaster<S extends MasterSection>(): UseMutationResult<
  MasterWriteResponse,
  Error,
  UpdateMasterArgs<S>
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ section, payload }: UpdateMasterArgs<S>) =>
      apiPut<MasterSectionPayload<S>, MasterWriteResponse>(
        `/api/master/${section}`,
        payload,
      ),
    onSuccess: (_data, { section }) => {
      void qc.invalidateQueries({ queryKey: queryKeys.master() });
      void qc.invalidateQueries({ queryKey: queryKeys.masterSection(section) });
    },
  });
}

interface UploadMasterArgs {
  section: MasterSection;
  file: File;
}

export function useUploadMaster(): UseMutationResult<
  MasterWriteResponse,
  Error,
  UploadMasterArgs
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ section, file }: UploadMasterArgs) =>
      apiPostMultipart<MasterWriteResponse>(
        `/api/master/${section}/upload`,
        file,
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.master() });
    },
  });
}

// ── Benchmark.md ─────────────────────────────────────────────────────────

export function useBenchmark(): UseQueryResult<BenchmarkResponse> {
  return useQuery({
    queryKey: queryKeys.benchmark(),
    queryFn: ({ signal }) =>
      apiGet<BenchmarkResponse>('/api/master/benchmark', signal),
  });
}

interface UpdateBenchmarkArgs {
  text: string;
  /** Current SHA-256 version from a prior GET, sent as If-Match. Optional. */
  ifMatch?: string;
}

export function useUpdateBenchmark(): UseMutationResult<
  BenchmarkResponse,
  Error,
  UpdateBenchmarkArgs
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ text, ifMatch }: UpdateBenchmarkArgs) =>
      apiPut<BenchmarkPayload, BenchmarkResponse>(
        '/api/master/benchmark',
        { text },
        ifMatch ? { ifMatch: `"${ifMatch}"` } : undefined,
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.benchmark() });
    },
  });
}

// ── Artifacts ────────────────────────────────────────────────────────────

interface UseArtifactArgs {
  slug: string;
  runId: string;
  kind: string;
}

export function useArtifact({
  slug,
  runId,
  kind,
}: UseArtifactArgs): UseQueryResult<ArtifactEnvelope> {
  return useQuery({
    queryKey: queryKeys.artifact(slug, runId, kind),
    queryFn: ({ signal }) =>
      apiGet<ArtifactEnvelope>(
        `/api/applications/${encodeURIComponent(slug)}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(kind)}`,
        signal,
      ),
    enabled: slug.length > 0 && runId.length > 0 && kind.length > 0,
  });
}

interface PutArtifactArgs {
  slug: string;
  runId: string;
  kind: string;
  body: PutArtifactBody;
  /** Current version int from a prior GET, sent as If-Match. */
  ifMatch?: number;
}

export function usePutArtifact(): UseMutationResult<
  ArtifactEnvelope,
  Error,
  PutArtifactArgs
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, runId, kind, body, ifMatch }: PutArtifactArgs) =>
      apiPut<PutArtifactBody, ArtifactEnvelope>(
        `/api/applications/${encodeURIComponent(slug)}/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(kind)}`,
        body,
        ifMatch !== undefined ? { ifMatch: `"${ifMatch}"` } : undefined,
      ),
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({
        queryKey: queryKeys.artifact(vars.slug, vars.runId, vars.kind),
      });
      void qc.invalidateQueries({
        queryKey: queryKeys.application(vars.slug),
      });
    },
  });
}

// ── Snapshots ────────────────────────────────────────────────────────────

interface SnapshotArgs {
  slug: string;
  runId: string;
  body?: SnapshotRequest;
}

export function useSnapshotRun(): UseMutationResult<
  SnapshotResponse,
  Error,
  SnapshotArgs
> {
  return useMutation({
    mutationFn: ({ slug, runId, body }: SnapshotArgs) =>
      apiPost<SnapshotRequest, SnapshotResponse>(
        `/api/applications/${encodeURIComponent(slug)}/runs/${encodeURIComponent(runId)}/snapshot`,
        body ?? {},
      ),
  });
}

// ── Application mutations (create / re-run) ──────────────────────────────

export interface CreateApplicationBody {
  jd_url?: string;
  jd_text?: string;
  jd_file_b64?: string;
  skip_confirmations?: boolean;
  force?: boolean;
  verbosity?: '--quiet' | '--normal' | '--verbose';
}

export interface CreateApplicationResponse {
  slug: string;
  run_id: string;
  events_url: string;
}

export function useCreateApplication(): UseMutationResult<
  CreateApplicationResponse,
  Error,
  CreateApplicationBody
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateApplicationBody) =>
      apiPost<CreateApplicationBody, CreateApplicationResponse>(
        '/api/applications',
        body,
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.applications() });
    },
  });
}

export interface RerunApplicationBody {
  skip_confirmations?: boolean;
  force?: boolean;
  verbosity?: '--quiet' | '--normal' | '--verbose';
}

export interface RerunApplicationResponse {
  slug: string;
  run_id: string;
  events_url: string;
}

export function useRerunApplication(): UseMutationResult<
  RerunApplicationResponse,
  Error,
  { slug: string; body?: RerunApplicationBody }
> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, body }) =>
      apiPost<RerunApplicationBody, RerunApplicationResponse>(
        `/api/applications/${encodeURIComponent(slug)}/run`,
        body ?? {},
      ),
    onSuccess: (_data, { slug }) => {
      void qc.invalidateQueries({ queryKey: queryKeys.applications() });
      void qc.invalidateQueries({ queryKey: queryKeys.application(slug) });
    },
  });
}

// ── SSE event stream ─────────────────────────────────────────────────────

export interface UseEventStreamOptions {
  verbosity?: Verbosity;
  reconnect?: boolean;
  /** Cap how many recent events the hook keeps in memory. Default 200. */
  maxEvents?: number;
}

export interface UseEventStreamResult {
  events: SseEvent[];
  isOpen: boolean;
  error: unknown | null;
  /** Close the stream early. */
  close: () => void;
}

/**
 * Subscribe to /api/applications/{slug}/events.
 *
 * Auto-reconnects on close (with exponential backoff in events.ts). The hook
 * exposes the recent event tail and the live open/error state.
 */
export function useEventStream(
  slug: string | null,
  opts: UseEventStreamOptions = {},
): UseEventStreamResult {
  const [events, setEvents] = useState<SseEvent[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [error, setError] = useState<unknown | null>(null);
  const closerRef = useRef<(() => void) | null>(null);
  const maxEvents = opts.maxEvents ?? 200;

  useEffect(() => {
    if (!slug) {
      setEvents([]);
      setIsOpen(false);
      return;
    }
    setEvents([]);
    setIsOpen(false);
    setError(null);

    const close = openEventStream(slug, {
      verbosity: opts.verbosity ?? 'verbose',
      reconnect: opts.reconnect ?? true,
      onOpen: () => setIsOpen(true),
      onError: (err) => {
        setIsOpen(false);
        setError(err);
      },
      onEvent: (e) => {
        setEvents((prev) => {
          const next = prev.length >= maxEvents ? prev.slice(prev.length - maxEvents + 1) : prev;
          return [...next, e];
        });
      },
      onClose: () => setIsOpen(false),
    });
    closerRef.current = close;
    return () => {
      close();
      closerRef.current = null;
    };
  }, [slug, opts.verbosity, opts.reconnect, maxEvents]);

  return {
    events,
    isOpen,
    error,
    close: () => closerRef.current?.(),
  };
}
