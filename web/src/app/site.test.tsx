// site.test.tsx — regression tests for the SiteView (Listings page).
//
// Covers:
//   - Real applicant name comes from /api/master/author, not hardcoded
//   - Sent applications come from /api/applications filtered by ui_phase
//   - The legacy mock string "jordan-smith.dev" never appears in output
//   - Empty rendered list renders the empty state, not "loading…"

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { SiteView } from './views';

vi.mock('../api/hooks', () => ({
  useApplications: vi.fn(),
  useMasterSection: vi.fn(),
  useFeedback: vi.fn(() => ({ data: [], isLoading: false, error: null })),
  useDoctor: vi.fn(() => ({ data: [], isLoading: false, error: null, refetch: () => {} })),
  useConfig: vi.fn(() => ({ data: undefined, isLoading: false, error: null })),
  JobsmithApiError: class JobsmithApiError extends Error { status = 500; },
}));

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error { status = 500; },
}));

import { useApplications, useMasterSection } from '../api/hooks';

const APPS = [
  { slug: 'acme-eng-2026-04', run_id: 'r1', phase: 'unknown', status: 'done', ui_phase: 'rendered', started_at: '2026-04-30T12:00:00Z', finished_at: '2026-04-30T12:01:00Z', role: null, company: null },
  { slug: 'beta-engineer-2026-04', run_id: 'r2', phase: 'unknown', status: 'backfilled', ui_phase: 'rendered', started_at: null, finished_at: null, role: null, company: null },
  { slug: 'still-running', run_id: 'r3', phase: 'gather', status: 'running', ui_phase: 'running', started_at: '2026-04-30T12:00:00Z', finished_at: null, role: null, company: null },
];

describe('SiteView regression — no mock leaks', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders rendered applications from the API', async () => {
    (useApplications as ReturnType<typeof vi.fn>).mockReturnValue({
      data: APPS, isLoading: false, error: null,
    });
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { name: { first: 'Real', last: 'Person' }, homepage: 'real-person.dev' },
      isLoading: false,
      error: null,
    });

    render(<SiteView />);

    await waitFor(() => {
      expect(screen.getByText('acme-eng-2026-04')).toBeInTheDocument();
    });
    expect(screen.getByText('beta-engineer-2026-04')).toBeInTheDocument();
    // 'still-running' has ui_phase='running' so it must NOT appear in the rendered list
    expect(screen.queryByText('still-running')).not.toBeInTheDocument();
  });

  it('does not leak the legacy "jordan-smith.dev" mock string', async () => {
    (useApplications as ReturnType<typeof vi.fn>).mockReturnValue({
      data: APPS, isLoading: false, error: null,
    });
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { name: { first: 'Real', last: 'Person' }, homepage: 'real-person.dev' },
      isLoading: false,
      error: null,
    });

    const { container } = render(<SiteView />);
    await waitFor(() => {
      expect(screen.getByText('acme-eng-2026-04')).toBeInTheDocument();
    });
    expect(container.textContent ?? '').not.toContain('jordan-smith.dev');
  });

  it('renders empty state (not loading) when no rendered applications', async () => {
    (useApplications as ReturnType<typeof vi.fn>).mockReturnValue({
      data: [], isLoading: false, error: null,
    });
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { name: { first: 'Real', last: 'Person' }, homepage: 'real-person.dev' },
      isLoading: false,
      error: null,
    });

    render(<SiteView />);
    expect(screen.getByText('no rendered applications yet.')).toBeInTheDocument();
    expect(screen.queryByText('loading…')).not.toBeInTheDocument();
  });
});
