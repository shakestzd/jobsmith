// application.test.tsx — DOD-pinned tests for the application-detail
// re-run-apply behavior (feat-d6b1e167, GH#50).
//
// Locks in:
// - For an already-complete slug (status=done/rendered/backfilled), the
//   re-run button is labelled "force re-run apply" and the click invokes
//   postApplication with `{ force: true }`.
// - For an in-flight or failed slug, the button is labelled "re-run apply"
//   and postApplication is invoked WITHOUT force (or with force=false).
//
// These tests guard against the silent-failure mode that #50 reported:
// the server returned 201, opened SSE, then aborted with "Application
// already complete. Re-run with --force to start over." because the UI
// gave the user no way to pass --force.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ApplicationDetail } from './application';

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  postApplication: vi.fn(),
  buildEventsUrl: vi.fn().mockReturnValue('http://localhost/events-noop'),
  redactSensitive: (s: string) => s,
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

import { apiGet, postApplication } from '../api/client';

// Mock EventSource — jsdom doesn't ship one, and we don't need real SSE here.
class FakeEventSource {
  readyState = 1;
  static CLOSED = 2;
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener() {}
  removeEventListener() {}
  close() {
    this.readyState = 2;
  }
  onerror: ((e: Event) => void) | null = null;
}
(globalThis as { EventSource?: unknown }).EventSource = FakeEventSource;

const BASE_API_DETAIL = {
  slug: 'acme-eng-2026-04',
  run_id: 'run-1',
  phase: 'render',
  status: 'done',
  ui_phase: 'rendered',
  started_at: '2026-04-30T12:00:00Z',
  finished_at: '2026-04-30T12:01:30Z',
  role: 'Engineer',
  company: 'Acme',
  artifacts: [],
};

describe('ApplicationDetail re-run button (feat-d6b1e167)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (postApplication as ReturnType<typeof vi.fn>).mockResolvedValue({
      slug: 'acme-eng-2026-04',
      run_id: 'run-2',
    });
  });

  it('shows "force re-run apply" label for a slug whose status is "done"', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /force re-run apply/i })).toBeInTheDocument();
    });
  });

  it('shows "force re-run apply" label for a slug whose status is "rendered"', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'rendered',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /force re-run apply/i })).toBeInTheDocument();
    });
  });

  it('shows "force re-run apply" label for a slug whose status is "backfilled"', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'backfilled',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /force re-run apply/i })).toBeInTheDocument();
    });
  });

  it('shows plain "re-run apply" label when the slug has not completed', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'failed',
      finished_at: null,
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^re-run apply$/i })).toBeInTheDocument();
    });
    // And the "force" variant must NOT be present.
    expect(screen.queryByRole('button', { name: /force re-run apply/i })).toBeNull();
  });

  it('clicking the force button invokes postApplication with force=true', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(postApplication).toHaveBeenCalledWith(
        expect.any(String),
        'acme-eng-2026-04',
        { force: true },
      );
    });
  });

  it('clicking the plain re-run button invokes postApplication with force=false', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'failed',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /^re-run apply$/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(postApplication).toHaveBeenCalledWith(
        expect.any(String),
        'acme-eng-2026-04',
        { force: false },
      );
    });
  });
});
