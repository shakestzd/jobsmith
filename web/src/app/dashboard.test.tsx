// dashboard.test.tsx — regression tests for the Dashboard view.
//
// Covers:
//   - null role/company render as the em-dash placeholder, never "TODO"
//   - "avg apply time" stat is computed from finished/started timestamps,
//     not displayed as a TODO string

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { Dashboard } from './dashboard';

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  postApplication: vi.fn(),
  buildEventsUrl: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

import { apiGet } from '../api/client';

const BASE_ROW = {
  slug: 'acme-eng-2026-04',
  run_id: 'run-1',
  phase: 'unknown',
  status: 'backfilled',
  ui_phase: 'rendered',
  started_at: '2026-04-30T12:00:00Z',
  finished_at: '2026-04-30T12:01:30Z',
  role: null as string | null,
  company: null as string | null,
};

describe('Dashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders em-dash for null role/company on backfilled rows', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue([BASE_ROW]);

    render(<Dashboard openApp={() => {}} openNew={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText('acme-eng-2026-04')).toBeInTheDocument();
    });
    // role and company columns render as "—" when null
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });

  it('does not leak the literal string "TODO" anywhere', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue([BASE_ROW]);
    const { container } = render(<Dashboard openApp={() => {}} openNew={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('acme-eng-2026-04')).toBeInTheDocument();
    });
    expect(container.textContent ?? '').not.toMatch(/TODO/);
  });

  it('computes avg apply time from finished/started timestamps', async () => {
    const rows = [
      { ...BASE_ROW, slug: 'a-1', started_at: '2026-04-30T12:00:00Z', finished_at: '2026-04-30T12:01:00Z' }, // 60s
      { ...BASE_ROW, slug: 'a-2', started_at: '2026-04-30T12:00:00Z', finished_at: '2026-04-30T12:03:00Z' }, // 180s
    ];
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(rows);
    render(<Dashboard openApp={() => {}} openNew={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('a-1')).toBeInTheDocument();
    });
    // avg = (60+180)/2 = 120s = 2m
    expect(screen.getByText('2m')).toBeInTheDocument();
  });

  it('renders em-dash for avg apply time when no rows have both timestamps', async () => {
    const rows = [
      { ...BASE_ROW, started_at: null, finished_at: null },
    ];
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(rows);
    render(<Dashboard openApp={() => {}} openNew={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('acme-eng-2026-04')).toBeInTheDocument();
    });
    // The avg apply time stat tile shows "—"
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThan(0);
  });

  // ── feat-aba75dae anti-regression: dead "import existing" button removed ──

  it('does NOT render the decorative "import existing" button (feat-aba75dae)', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue([BASE_ROW]);
    render(<Dashboard openApp={() => {}} openNew={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('acme-eng-2026-04')).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /import existing/i })).toBeNull();
  });
});
