// master-author.test.tsx — ETag save round-trip tests for AuthorTab (feat-b28e9206).
//
// Grep tokens:
//   FROM_API_author_8h7p — appears in mocked GET data, verified in DOM
//
// Covers:
//   (a) GET data reaches DOM
//   (b) Save fires PUT with correct body + If-Match header
//   (c) 412 conflict: local edits preserved + both action buttons
//   (d) 404 missing_in_db: suggestion <code> snippet appears

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MasterContent } from './master';

const { MockApiError } = vi.hoisted(() => {
  class MockApiError extends Error {
    status: number;
    constructor(msg: string, status: number) {
      super(msg);
      this.status = status;
      this.name = 'JobsmithApiError';
    }
  }
  return { MockApiError };
});

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiGetWithMeta: vi.fn(),
  formatDetail: vi.fn((_raw: unknown, fallback: string) => fallback),
  JobsmithApiError: MockApiError,
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
  JobsmithApiError: MockApiError,
}));

import { apiGet, apiPost, apiPut } from '../api/client';
import { useMasterSection, useMasterSectionWithMeta } from '../api/hooks';

const MOCK_PAYLOAD = {
  work: [{ title: 'Engineer', location: 'Acme' }],
  skill: [],
  education: [],
  author: { name: 'A B' },
};

// Author fixture with grep token FROM_API_author_8h7p
const AUTHOR_DATA = {
  name: 'FROM_API_author_8h7p',
  email: 'test@example.com',
  phone: '+1-555-0100',
  address: 'San Francisco, CA',
  position: 'Staff Engineer',
  contacts: [],
};

describe('AuthorTab — ETag save round-trip (feat-b28e9206)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_PAYLOAD);
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, errors: [] });
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined, isLoading: false, error: null,
    });
  });

  it('(a) GET data with grep token FROM_API_author_8h7p reaches the DOM', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: AUTHOR_DATA,
      etag: '"etag-author-abc"',
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/author\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_author_8h7p')).toBeInTheDocument();
    });
  });

  it('(b) editing name and clicking Save fires PUT with edited name + If-Match', async () => {
    const refetchMock = vi.fn();
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: AUTHOR_DATA,
      etag: '"etag-author-abc"',
      isLoading: false,
      error: null,
      refetch: refetchMock,
    });
    (apiPut as ReturnType<typeof vi.fn>).mockResolvedValue({
      section: 'author', path: 'db:author', bytes_written: 10,
    });

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/author\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_author_8h7p')).toBeInTheDocument();
    });

    // Edit name field
    const nameInput = screen.getByLabelText('name');
    fireEvent.change(nameInput, { target: { value: 'FROM_API_author_8h7p_edited' } });

    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith(
        '/api/master/author',
        expect.objectContaining({
          author: expect.arrayContaining([
            expect.objectContaining({ name: 'FROM_API_author_8h7p_edited' }),
          ]),
        }),
        expect.objectContaining({ ifMatch: '"etag-author-abc"' }),
      );
    });
  });

  it('(c) 412: local edits preserved + both action buttons', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: AUTHOR_DATA,
      etag: '"etag-author-abc"',
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(new MockApiError('ETag mismatch', 412));

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/author\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_author_8h7p')).toBeInTheDocument();
    });

    // Edit to make dirty
    const nameInput = screen.getByLabelText('name');
    fireEvent.change(nameInput, { target: { value: 'local_edit_author' } });

    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByText(/section changed elsewhere/i)).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: /discard local \+ refresh/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /overwrite anyway/i })).toBeInTheDocument();
    // Local edits still present
    expect(screen.getByDisplayValue('local_edit_author')).toBeInTheDocument();
  });

  it('(d) 404 missing_in_db: suggestion code snippet appears', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: AUTHOR_DATA,
      etag: '"etag-author-abc"',
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(new MockApiError('missing_in_db', 404));

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/author\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_author_8h7p')).toBeInTheDocument();
    });

    // Make dirty
    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'some_edit' } });

    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
      expect(screen.getByRole('code')).toBeInTheDocument();
    });
  });
});
