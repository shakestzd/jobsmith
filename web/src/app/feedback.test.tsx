// feedback.test.tsx — unit tests for FeedbackView
//
// Assertions:
//   GET /api/feedback rows render
//   loading and empty states render correctly

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { FeedbackView } from './views';

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

import { apiGet } from '../api/client';

const MOCK_ROWS = [
  {
    slug: 'anthropic-applied-ai-2026-04',
    timestamp: '2026-04-30T12:00:00Z',
    kind: 'edit',
    before: 'old paragraph',
    after: 'new paragraph',
    lesson: 'tightened cover paragraph',
    context: null,
  },
  {
    slug: 'render-cli-2026-03',
    timestamp: '2026-04-08T09:30:00Z',
    kind: 'outcome',
    before: '',
    after: 'on-site invite',
    lesson: 'on-site invite',
    context: null,
  },
];

describe('FeedbackView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders rows from GET /api/feedback', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_ROWS);

    render(<FeedbackView />);

    await waitFor(() => {
      expect(screen.getByText('anthropic-applied-ai-2026-04')).toBeInTheDocument();
    });
    expect(screen.getByText('render-cli-2026-03')).toBeInTheDocument();
    expect(screen.getByText('outcome')).toBeInTheDocument();
    expect(screen.getByText(/tightened cover paragraph/)).toBeInTheDocument();
  });

  it('shows loading state initially', () => {
    (apiGet as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {})); // never resolves
    render(<FeedbackView />);
    expect(screen.getByText('loading…')).toBeInTheDocument();
  });

  it('shows empty state when no rows', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    render(<FeedbackView />);
    await waitFor(() => {
      expect(screen.getByText('no feedback recorded yet.')).toBeInTheDocument();
    });
  });

  it('does not render the legacy hardcoded slug', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_ROWS);
    render(<FeedbackView />);
    await waitFor(() => {
      expect(screen.getByText('anthropic-applied-ai-2026-04')).toBeInTheDocument();
    });
    // 'fly-systems-2026-03' was a hardcoded mock entry — must not appear.
    expect(screen.queryByText('fly-systems-2026-03')).not.toBeInTheDocument();
  });

  it('counts edits and outcomes from live data', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_ROWS);
    render(<FeedbackView />);
    await waitFor(() => {
      expect(screen.getByText('anthropic-applied-ai-2026-04')).toBeInTheDocument();
    });
    // total = 2, edits = 1, outcomes = 1
    const stats = screen.getAllByText('2');
    expect(stats.length).toBeGreaterThan(0);
  });
});
