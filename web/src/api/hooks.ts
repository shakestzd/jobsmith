// hooks.ts — Lightweight data-fetching hooks using plain useState/useEffect.
//
// No external query library. Each hook manages its own loading/error/data state.
// Re-fetching on mount is intentional — no cache invalidation is in scope here.
//
// Exports:
//   useApplications          — GET /api/applications → ApplicationRow[]
//   useApplication           — GET /api/applications/{slug} → ApplicationDetail
//                              Retries on 404 up to 5 times (200ms × attempt backoff)
//                              to survive the POST→GET race after modal-launched runs.
//   useMasterSection         — GET /api/master/{section} → section data
//   useMasterSectionWithMeta — GET /api/master/{section} → { data, etag, isLoading, error, refetch }

import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiGetWithMeta, JobsmithApiError } from './client';
import type {
  ApplicationRow,
  ApplicationDetail,
  MasterWorkRole,
  MasterSkillGroup,
  MasterEducationEntry,
  MasterAuthor,
  MasterBenchmark,
  MasterSectionData,
  JobsmithConfig,
  FeedbackRecord,
  DoctorCheckResult,
} from './types';

// Re-export so callers can import from one place.
export { JobsmithApiError };

// ── Hook return shape ────────────────────────────────────────────────────

interface UseQueryResult<T> {
  data: T | undefined;
  isLoading: boolean;
  error: Error | null;
}

// ── Generic fetcher hook ─────────────────────────────────────────────────

function useFetch<T>(path: string | null): UseQueryResult<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [isLoading, setIsLoading] = useState<boolean>(path !== null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (path === null) return;
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    apiGet<T>(path)
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setIsLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err : new Error(String(err)));
          setIsLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [path]);

  return { data, isLoading, error };
}

// ── Applications ─────────────────────────────────────────────────────────

export function useApplications(): UseQueryResult<ApplicationRow[]> {
  return useFetch<ApplicationRow[]>('/api/applications');
}

// Maximum number of 404-specific retries after the initial request.
// After POST /api/applications returns 201, the detail endpoint may return 404
// for up to ~1-2s while the server persists the new run. We retry rather than
// surface an error immediately (feat-092c5a2c, GH#60).
const APPLICATION_404_MAX_RETRIES = 5;
// Initial retry delay in ms. Each retry multiplies by its attempt index:
// attempt 1 → 200ms, 2 → 400ms, 3 → 600ms, 4 → 800ms, 5 → 1000ms.
const APPLICATION_404_RETRY_MS = 200;

/**
 * Fetch a single ApplicationDetail with automatic 404 retry.
 *
 * Exported for unit-testing the retry logic in isolation.
 * The `delayMs` parameter overrides the per-attempt delay; pass 0 in tests.
 */
export async function fetchApplicationWithRetry(
  slug: string,
  signal: { cancelled: boolean },
  delayMs: number = APPLICATION_404_RETRY_MS,
): Promise<ApplicationDetail> {
  const path = `/api/applications/${encodeURIComponent(slug)}`;
  let lastErr: Error | null = null;

  for (let attempt = 0; attempt <= APPLICATION_404_MAX_RETRIES; attempt += 1) {
    if (signal.cancelled) throw new Error('cancelled');

    if (attempt > 0) {
      // Linear backoff: delayMs × attempt.
      await new Promise<void>((resolve) => {
        setTimeout(resolve, delayMs * attempt);
      });
      if (signal.cancelled) throw new Error('cancelled');
    }

    try {
      return await apiGet<ApplicationDetail>(path);
    } catch (err: unknown) {
      if (signal.cancelled) throw new Error('cancelled');
      // Only retry on 404 — all other status codes surface immediately.
      if (err instanceof JobsmithApiError && err.status === 404) {
        lastErr = err;
        // Continue to next attempt.
      } else {
        throw err;
      }
    }
  }

  // All retries exhausted — re-throw the last 404 error.
  throw lastErr ?? new Error('Not Found');
}

export function useApplication(slug: string): UseQueryResult<ApplicationDetail> {
  const [data, setData] = useState<ApplicationDetail | undefined>(undefined);
  const [isLoading, setIsLoading] = useState<boolean>(Boolean(slug));
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    // Reset on every slug change so the component never renders the previous
    // slug's data under the new slug while the new fetch is in flight
    // (roborev job 947 HIGH). The empty-slug branch also clears state — a
    // route that briefly drops the slug must not retain prior detail.
    setData(undefined);
    setError(null);

    if (!slug) {
      setIsLoading(false);
      return;
    }

    setIsLoading(true);
    const signal = { cancelled: false };

    fetchApplicationWithRetry(slug, signal)
      .then((result) => {
        if (!signal.cancelled) {
          setData(result);
          setIsLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!signal.cancelled) {
          setError(err instanceof Error ? err : new Error(String(err)));
          setIsLoading(false);
        }
      });

    return () => { signal.cancelled = true; };
  }, [slug]);

  return { data, isLoading, error };
}

// ── Master sections ──────────────────────────────────────────────────────

// Overloads for discriminated return types per section.
export function useMasterSection(section: 'work'): UseQueryResult<MasterWorkRole[]>;
export function useMasterSection(section: 'skill'): UseQueryResult<MasterSkillGroup[]>;
export function useMasterSection(section: 'education'): UseQueryResult<MasterEducationEntry[]>;
export function useMasterSection(section: 'author'): UseQueryResult<MasterAuthor | null>;
export function useMasterSection(section: 'benchmark'): UseQueryResult<MasterBenchmark>;
export function useMasterSection<K extends keyof MasterSectionData>(
  section: K,
): UseQueryResult<MasterSectionData[K]> {
  return useFetch<MasterSectionData[K]>(`/api/master/${section}`);
}

// ── Master sections with ETag metadata ──────────────────────────────────

interface UseQueryWithMetaResult<T> {
  data: T | undefined;
  etag: string | null;
  isLoading: boolean;
  error: Error | null;
  refetch: () => void;
}

/**
 * Like useMasterSection but also exposes `etag` and `refetch`.
 * Used by SkillTab / EducationTab / AuthorTab / BenchmarkTab for ETag-based PUT round-trips.
 * Do NOT modify useMasterSection — it has 6+ callers.
 */
export function useMasterSectionWithMeta(
  section: 'skill',
): UseQueryWithMetaResult<MasterSkillGroup[]>;
export function useMasterSectionWithMeta(
  section: 'education',
): UseQueryWithMetaResult<MasterEducationEntry[]>;
export function useMasterSectionWithMeta(
  section: 'author',
): UseQueryWithMetaResult<MasterAuthor | null>;
export function useMasterSectionWithMeta(
  section: 'benchmark',
): UseQueryWithMetaResult<MasterBenchmark>;
export function useMasterSectionWithMeta(
  section: 'work',
): UseQueryWithMetaResult<MasterSectionData['work']>;
export function useMasterSectionWithMeta<K extends keyof MasterSectionData>(
  section: K,
): UseQueryWithMetaResult<MasterSectionData[K]> {
  const [tick, setTick] = useState(0);
  const [data, setData] = useState<MasterSectionData[K] | undefined>(undefined);
  const [etag, setEtag] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    apiGetWithMeta<MasterSectionData[K]>(`/api/master/${section}`)
      .then(({ data: result, etag: newEtag }) => {
        if (!cancelled) {
          setData(result);
          setEtag(newEtag);
          setIsLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err : new Error(String(err)));
          setIsLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [section, tick]);

  const refetch = useCallback(() => setTick((t) => t + 1), []);
  return { data, etag, isLoading, error, refetch };
}

// ── Config ───────────────────────────────────────────────────────────────

export function useConfig(): UseQueryResult<JobsmithConfig> {
  return useFetch<JobsmithConfig>('/api/config');
}

// ── Feedback ─────────────────────────────────────────────────────────────

export function useFeedback(): UseQueryResult<FeedbackRecord[]> {
  return useFetch<FeedbackRecord[]>('/api/feedback');
}

// ── Doctor (with refetch) ────────────────────────────────────────────────

interface UseRefetchableQueryResult<T> extends UseQueryResult<T> {
  refetch: () => void;
}

export function useDoctor(): UseRefetchableQueryResult<DoctorCheckResult[]> {
  const [tick, setTick] = useState(0);
  const [data, setData] = useState<DoctorCheckResult[] | undefined>(undefined);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    apiGet<DoctorCheckResult[]>('/api/doctor')
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setIsLoading(false);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err : new Error(String(err)));
          setIsLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [tick]);

  const refetch = useCallback(() => setTick((t) => t + 1), []);
  return { data, isLoading, error, refetch };
}
