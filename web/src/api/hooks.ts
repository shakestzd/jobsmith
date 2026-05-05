// hooks.ts — Lightweight data-fetching hooks using plain useState/useEffect.
//
// No external query library. Each hook manages its own loading/error/data state.
// Re-fetching on mount is intentional — no cache invalidation is in scope here.
//
// Exports:
//   useApplications    — GET /api/applications → ApplicationRow[]
//   useApplication     — GET /api/applications/{slug} → ApplicationDetail
//   useMasterSection   — GET /api/master/{section} → section data

import { useCallback, useEffect, useState } from 'react';
import { apiGet, JobsmithApiError } from './client';
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

export function useApplication(slug: string): UseQueryResult<ApplicationDetail> {
  return useFetch<ApplicationDetail>(slug ? `/api/applications/${encodeURIComponent(slug)}` : null);
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
