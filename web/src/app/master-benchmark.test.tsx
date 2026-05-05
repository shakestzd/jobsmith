// master-benchmark.test.tsx — TDD tests for BenchmarkTab save round-trip.
//
// Covers:
//   (a) GET hydration: unique token reaches rendered preview; Save fires PUT
//       with correct body and If-Match header.
//   (b) 412 conflict: pending edits preserved, banner shown; "overwrite anyway"
//       fires second PUT using server version fetched after 412.
//   (c) 404 missing_in_db: suggestion code snippet appears.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MasterContent } from './master';

// ── Mocks ─────────────────────────────────────────────────────────────────

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiGetWithMeta: vi.fn(),
  apiDelete: vi.fn(),
  formatDetail: vi.fn((_raw: unknown, fallback: string) => fallback),
  JobsmithApiError: class JobsmithApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = 'JobsmithApiError';
      this.status = status;
    }
  },
}));

vi.mock('../api/hooks', () => ({
  useMasterSection: vi.fn(() => ({ data: undefined, isLoading: false, error: null })),
  useMasterSectionWithMeta: vi.fn(() => ({
    data: undefined,
    etag: null,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  })),
  JobsmithApiError: class JobsmithApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = 'JobsmithApiError';
      this.status = status;
    }
  },
}));

import { apiGet, apiPost, apiPut, apiGetWithMeta } from '../api/client';
import { useMasterSection, useMasterSectionWithMeta } from '../api/hooks';

const BENCHMARK_TOKEN = 'FROM_API_benchmark_n7v3';
const BENCHMARK_TEXT = `${BENCHMARK_TOKEN} — write in a direct, senior-engineer voice.`;
const BENCHMARK_VERSION = 'v1sha256abc';

// Navigate to benchmark tab
function renderAndClickBenchmark() {
  render(<MasterContent />);
  const benchmarkTab = screen.getByText(/benchmark\.md/i);
  fireEvent.click(benchmarkTab);
}

// ── (a) GET hydration + Save ──────────────────────────────────────────────

describe('BenchmarkTab — (a) GET hydration and Save', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined,
      isLoading: false,
      error: null,
    });
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      work: [], skill: [], education: [], author: null,
    });
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, errors: [] });
  });

  it('renders unique GET token in the markdown preview', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { text: BENCHMARK_TEXT, version: BENCHMARK_VERSION },
      etag: BENCHMARK_VERSION,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderAndClickBenchmark();

    await waitFor(() => {
      const preview = screen.getByLabelText('benchmark markdown preview');
      expect(preview.textContent).toContain(BENCHMARK_TOKEN);
    });
  });

  it('fires apiPut with edited text and If-Match header on Save click', async () => {
    const refetch = vi.fn();
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { text: BENCHMARK_TEXT, version: BENCHMARK_VERSION },
      etag: BENCHMARK_VERSION,
      isLoading: false,
      error: null,
      refetch,
    });
    (apiPut as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: 'edited text',
      version: 'v2sha256',
    });

    renderAndClickBenchmark();

    const textarea = screen.getByLabelText('benchmark markdown source');
    fireEvent.change(textarea, { target: { value: 'edited text' } });

    const saveBtn = screen.getByRole('button', { name: /^save$/i });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith(
        '/api/master/benchmark',
        { text: 'edited text' },
        { ifMatch: BENCHMARK_VERSION },
      );
    });
  });

  it('shows "saved" pill after successful save', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { text: BENCHMARK_TEXT, version: BENCHMARK_VERSION },
      etag: BENCHMARK_VERSION,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    (apiPut as ReturnType<typeof vi.fn>).mockResolvedValue({
      text: BENCHMARK_TEXT,
      version: 'v2sha256',
    });

    renderAndClickBenchmark();

    // Edit so Save becomes enabled
    fireEvent.change(screen.getByLabelText('benchmark markdown source'), {
      target: { value: 'edited text' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByText(/saved/i)).toBeInTheDocument();
    });
  });

  it('Save button is disabled when text is unchanged', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { text: BENCHMARK_TEXT, version: BENCHMARK_VERSION },
      etag: BENCHMARK_VERSION,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    renderAndClickBenchmark();

    await waitFor(() => {
      const saveBtn = screen.getByRole('button', { name: /^save$/i });
      expect(saveBtn).toBeDisabled();
    });
  });
});

// ── (b) 412 conflict path ─────────────────────────────────────────────────

describe('BenchmarkTab — (b) 412 conflict', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined,
      isLoading: false,
      error: null,
    });
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      work: [], skill: [], education: [], author: null,
    });
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, errors: [] });
  });

  it('preserves pending edits and shows conflict banner on 412', async () => {
    const { JobsmithApiError } = await import('../api/client');
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { text: BENCHMARK_TEXT, version: BENCHMARK_VERSION },
      etag: BENCHMARK_VERSION,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    const conflictErr = new JobsmithApiError('Precondition Failed', 412);
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(conflictErr);

    renderAndClickBenchmark();

    fireEvent.change(screen.getByLabelText('benchmark markdown source'), {
      target: { value: 'my unsaved changes' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByText(/section changed elsewhere/i)).toBeInTheDocument();
    });

    // Pending edits preserved in textarea
    expect(screen.getByLabelText('benchmark markdown source')).toHaveValue('my unsaved changes');
  });

  it('shows discard+refresh and overwrite-anyway buttons in conflict banner', async () => {
    const { JobsmithApiError } = await import('../api/client');
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { text: BENCHMARK_TEXT, version: BENCHMARK_VERSION },
      etag: BENCHMARK_VERSION,
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(
      new JobsmithApiError('Precondition Failed', 412),
    );

    renderAndClickBenchmark();
    fireEvent.change(screen.getByLabelText('benchmark markdown source'), {
      target: { value: 'pending' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /discard/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /overwrite/i })).toBeInTheDocument();
    });
  });

  it('overwrite-anyway refetches server version then fires second PUT with new If-Match', async () => {
    const { JobsmithApiError } = await import('../api/client');
    const refetch = vi.fn();
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { text: BENCHMARK_TEXT, version: BENCHMARK_VERSION },
      etag: BENCHMARK_VERSION,
      isLoading: false,
      error: null,
      refetch,
    });

    // First PUT → 412
    (apiPut as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new JobsmithApiError('Precondition Failed', 412))
      .mockResolvedValueOnce({ text: 'my edits', version: 'v3' });

    // Re-fetch after 412 returns the new server version
    (apiGetWithMeta as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { text: 'server version', version: 'v2server' },
      etag: 'v2server',
      status: 200,
    });

    renderAndClickBenchmark();
    fireEvent.change(screen.getByLabelText('benchmark markdown source'), {
      target: { value: 'my edits' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    // Wait for conflict banner
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /overwrite/i })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: /overwrite/i }));

    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledTimes(2);
      // Second PUT uses version from the re-fetch
      expect(apiPut).toHaveBeenLastCalledWith(
        '/api/master/benchmark',
        { text: 'my edits' },
        { ifMatch: 'v2server' },
      );
    });
  });
});

// ── (c) 404 missing_in_db ─────────────────────────────────────────────────

describe('BenchmarkTab — (c) 404 missing_in_db', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined,
      isLoading: false,
      error: null,
    });
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      work: [], skill: [], education: [], author: null,
    });
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, errors: [] });
  });

  it('shows suggestion code snippet when hook returns 404 missing_in_db error', async () => {
    const { JobsmithApiError } = await import('../api/client');
    const notFoundErr = new JobsmithApiError(
      'missing_in_db — jobsmith db load-master  # to backfill section',
      404,
    );
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined,
      etag: null,
      isLoading: false,
      error: notFoundErr,
      refetch: vi.fn(),
    });

    renderAndClickBenchmark();

    await waitFor(() => {
      const content = document.body.textContent ?? '';
      // The error message contains "missing_in_db" and/or the load-master suggestion
      expect(content).toMatch(/missing_in_db|load-master|backfill/i);
    });
  });

  it('shows suggestion snippet via SaveBar when save is attempted on a 404 section', async () => {
    const { JobsmithApiError } = await import('../api/client');
    const refetch = vi.fn();
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { text: BENCHMARK_TEXT, version: BENCHMARK_VERSION },
      etag: BENCHMARK_VERSION,
      isLoading: false,
      error: null,
      refetch,
    });

    // PUT returns 404
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(
      new JobsmithApiError('missing_in_db', 404),
    );

    renderAndClickBenchmark();
    fireEvent.change(screen.getByLabelText('benchmark markdown source'), {
      target: { value: 'edited' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      const content = document.body.textContent ?? '';
      expect(content).toMatch(/load-master|backfill/i);
    });
  });
});
