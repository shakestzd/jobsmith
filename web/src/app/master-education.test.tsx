// master-education.test.tsx — ETag save round-trip tests for EducationTab (feat-b28e9206).
//
// Grep tokens:
//   FROM_API_education_2k4m — appears in mocked GET data, verified in DOM
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

// Education fixture with grep token FROM_API_education_2k4m
const EDUCATION_DATA = [
  {
    title: 'FROM_API_education_2k4m',
    description: 'B.S. Computer Science',
    date: '2018',
    location: 'Cambridge, MA',
    details: [],
  },
];

describe('EducationTab — ETag save round-trip (feat-b28e9206)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_PAYLOAD);
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, errors: [] });
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined, isLoading: false, error: null,
    });
  });

  it('(a) GET data with grep token FROM_API_education_2k4m reaches the DOM', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: EDUCATION_DATA,
      etag: '"etag-edu-abc"',
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/education\.yml/i));

    await waitFor(() => {
      // institution field (title in API = FROM_API_education_2k4m) appears
      expect(screen.getByDisplayValue('FROM_API_education_2k4m')).toBeInTheDocument();
    });
  });

  it('(b) editing and clicking Save fires PUT with edited institution + If-Match', async () => {
    const refetchMock = vi.fn();
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: EDUCATION_DATA,
      etag: '"etag-edu-abc"',
      isLoading: false,
      error: null,
      refetch: refetchMock,
    });
    (apiPut as ReturnType<typeof vi.fn>).mockResolvedValue({
      section: 'education', path: 'db:education', bytes_written: 10,
    });

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/education\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_education_2k4m')).toBeInTheDocument();
    });

    // Edit institution field
    const institutionInput = screen.getByDisplayValue('FROM_API_education_2k4m');
    fireEvent.change(institutionInput, { target: { value: 'MIT_edited' } });

    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith(
        '/api/master/education',
        expect.arrayContaining([
          expect.objectContaining({ title: 'MIT_edited' }),
        ]),
        expect.objectContaining({ ifMatch: '"etag-edu-abc"' }),
      );
    });
  });

  it('(c) 412: local edits preserved + both action buttons', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: EDUCATION_DATA,
      etag: '"etag-edu-abc"',
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(new MockApiError('ETag mismatch', 412));

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/education\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_education_2k4m')).toBeInTheDocument();
    });

    // Edit then save
    fireEvent.change(screen.getByDisplayValue('FROM_API_education_2k4m'), {
      target: { value: 'local_edit_edu' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByText(/section changed elsewhere/i)).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: /discard local \+ refresh/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /overwrite anyway/i })).toBeInTheDocument();
    // Local edits still visible
    expect(screen.getByDisplayValue('local_edit_edu')).toBeInTheDocument();
  });

  it('(d) 404 missing_in_db: suggestion code snippet appears', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: EDUCATION_DATA,
      etag: '"etag-edu-abc"',
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(new MockApiError('missing_in_db', 404));

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/education\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_education_2k4m')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByDisplayValue('FROM_API_education_2k4m'), {
      target: { value: 'some_edit' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
      expect(screen.getByRole('code')).toBeInTheDocument();
    });
  });
});
