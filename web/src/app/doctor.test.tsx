// doctor.test.tsx — unit tests for DoctorView
//
// Assertions:
//   GET /api/doctor rows render with correct status badges
//   re-run button refetches
//   loading and error states render

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { DoctorView } from './views';

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

import { apiGet } from '../api/client';

const MOCK_CHECKS = [
  { name: 'claude CLI', status: 'pass', message: 'v1.4.0 at /usr/local/bin/claude' },
  { name: 'benchmark.md', status: 'warn', message: 'last edited 4 months ago' },
  { name: 'plugin/agents/', status: 'fail', message: 'directory missing' },
];

describe('DoctorView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders rows from GET /api/doctor with correct badges', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_CHECKS);

    render(<DoctorView />);

    await waitFor(() => {
      expect(screen.getByText('claude CLI')).toBeInTheDocument();
    });
    expect(screen.getByText('benchmark.md')).toBeInTheDocument();
    expect(screen.getByText('plugin/agents/')).toBeInTheDocument();
    expect(screen.getByText('ok')).toBeInTheDocument();
    expect(screen.getByText('warn')).toBeInTheDocument();
    expect(screen.getByText('fail')).toBeInTheDocument();
  });

  it('refetches when re-run button is clicked', async () => {
    const mock = apiGet as ReturnType<typeof vi.fn>;
    mock.mockResolvedValue(MOCK_CHECKS);

    render(<DoctorView />);

    await waitFor(() => {
      expect(mock).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole('button', { name: /re-run checks/i }));

    await waitFor(() => {
      expect(mock).toHaveBeenCalledTimes(2);
    });
  });

  it('shows loading state initially', () => {
    (apiGet as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    render(<DoctorView />);
    expect(screen.getByText('loading…')).toBeInTheDocument();
  });

  it('shows error state on fetch failure', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));
    render(<DoctorView />);
    await waitFor(() => {
      expect(screen.getByText(/failed to load checks: boom/)).toBeInTheDocument();
    });
  });

  it('does not render legacy hardcoded check names', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_CHECKS);
    render(<DoctorView />);
    await waitFor(() => {
      expect(screen.getByText('claude CLI')).toBeInTheDocument();
    });
    // 'private/feedback.db' was hardcoded — only appears now if the API returns it.
    expect(screen.queryByText('private/feedback.db')).not.toBeInTheDocument();
  });
});
