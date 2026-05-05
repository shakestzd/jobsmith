// master.test.tsx — unit tests for the validate button on MasterContent.
//
// Covers:
//   click → fetches /api/master then POSTs /api/master/validate
//   ok=true → "all sections valid."
//   ok=false → renders each error
//   network failure → renders error message

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MasterContent } from './master';

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

vi.mock('../api/hooks', () => ({
  useMasterSection: vi.fn(() => ({ data: undefined, isLoading: false, error: null })),
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

import { apiGet, apiPost } from '../api/client';

const MOCK_PAYLOAD = {
  work: [{ position: 'Engineer', company: 'Acme' }],
  skill: [],
  education: [],
  author: { name: { first: 'A', last: 'B' } },
};

describe('MasterContent validate button', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls /api/master then /api/master/validate on click', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_PAYLOAD);
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, errors: [] });

    render(<MasterContent />);
    fireEvent.click(screen.getByRole('button', { name: /validate/i }));

    await waitFor(() => {
      expect(apiGet).toHaveBeenCalledWith('/api/master');
      expect(apiPost).toHaveBeenCalledWith('/api/master/validate', MOCK_PAYLOAD);
    });
  });

  it('renders success message when ok=true', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_PAYLOAD);
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, errors: [] });

    render(<MasterContent />);
    fireEvent.click(screen.getByRole('button', { name: /validate/i }));

    await waitFor(() => {
      expect(screen.getByText('all sections valid.')).toBeInTheDocument();
    });
  });

  it('renders each error when ok=false', async () => {
    const errors = [
      { field: 'work[0].position', message: 'must not be empty' },
      { field: 'author.name', message: 'author must have a name' },
    ];
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_PAYLOAD);
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: false, errors });

    render(<MasterContent />);
    fireEvent.click(screen.getByRole('button', { name: /validate/i }));

    await waitFor(() => {
      expect(screen.getByText(/2 validation errors/)).toBeInTheDocument();
    });
    expect(screen.getByText('work[0].position')).toBeInTheDocument();
    expect(screen.getByText('author.name')).toBeInTheDocument();
  });

  it('renders failure on network error', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));

    render(<MasterContent />);
    fireEvent.click(screen.getByRole('button', { name: /validate/i }));

    await waitFor(() => {
      expect(screen.getByText(/validation request failed: boom/)).toBeInTheDocument();
    });
  });

  it('disables the button while validating', async () => {
    let resolveGet: (v: unknown) => void;
    (apiGet as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise((r) => { resolveGet = r; }),
    );

    render(<MasterContent />);
    const btn = screen.getByRole('button', { name: /validate/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /validating/i })).toBeDisabled();
    });

    resolveGet!(MOCK_PAYLOAD);
  });
});
