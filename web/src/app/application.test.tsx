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

// Default fixture has a URL set so the re-run button is enabled. Tests
// that exercise the URL-missing path override `url` to '' or null on the
// returned object.
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
  url: 'https://example.com/jobs/acme-eng',
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

  it('clicking force button passes the REAL url, never a placeholder (roborev job 944)', async () => {
    // Anti-regression: previously `handleReRun` used
    //   const url = app.url || `https://placeholder/${slug}`;
    // which would destructively force-restart a real run with a fake URL
    // when the API didn't expose the original URL. Now the button is
    // disabled when there's no URL, and when it IS clickable the real
    // URL is what flows to postApplication.
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
      url: 'https://real.example.com/jobs/clay-gtm-data-analyst',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(postApplication).toHaveBeenCalledWith(
        'https://real.example.com/jobs/clay-gtm-data-analyst',
        'acme-eng-2026-04',
        { force: true },
      );
    });
    // Confirm no placeholder URL was ever sent.
    const calls = (postApplication as ReturnType<typeof vi.fn>).mock.calls;
    for (const call of calls) {
      expect(call[0]).not.toMatch(/placeholder/);
    }
  });

  it('disables the re-run button when the API does not expose a URL (roborev job 944)', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
      url: '', // server has no URL on file for this slug
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    expect(btn).toBeDisabled();
  });

  it('disables the re-run button when the API URL field is missing entirely', async () => {
    // Older runs may not include the `url` key at all (pre-feat-d6b1e167
    // apply_runs rows). Treat undefined the same as empty string.
    const { url: _drop, ...detailWithoutUrl } = {
      ...BASE_API_DETAIL,
      status: 'failed',
    };
    void _drop;
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(detailWithoutUrl);
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /^re-run apply$/i });
    expect(btn).toBeDisabled();
    // No postApplication call should fire from the disabled click.
    fireEvent.click(btn);
    expect(postApplication).not.toHaveBeenCalled();
  });
});
