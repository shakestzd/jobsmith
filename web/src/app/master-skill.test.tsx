// master-skill.test.tsx — ETag save round-trip tests for SkillTab (feat-b28e9206).
//
// Grep tokens:
//   FROM_API_skill_9q2x — appears in mocked GET data, verified in DOM
//
// Covers:
//   (a) GET data reaches DOM via useMasterSectionWithMeta
//   (b) Save fires PUT with correct body + If-Match header
//   (c) 412 conflict: local edits preserved + banner with both action buttons
//       clicking "overwrite anyway" fires second PUT with no If-Match
//   (d) 404 missing_in_db: suggestion <code> snippet appears

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MasterContent } from './master';

// vi.mock is hoisted — factory must not reference any local variables.
// Use vi.hoisted to create mocks that can be referenced in factories.
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

// Skill fixture with grep token FROM_API_skill_9q2x
const SKILL_DATA = [
  { title: 'Languages', description: 'FROM_API_skill_9q2x', details: ['typed'] },
];

describe('SkillTab — ETag save round-trip (feat-b28e9206)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(MOCK_PAYLOAD);
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, errors: [] });
    (useMasterSection as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined, isLoading: false, error: null,
    });
  });

  it('(a) GET data with grep token FROM_API_skill_9q2x reaches the DOM', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: SKILL_DATA,
      etag: '"etag-skill-abc"',
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    render(<MasterContent />);
    // Navigate to skill tab
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_skill_9q2x')).toBeInTheDocument();
    });
  });

  it('(b) editing and clicking Save fires PUT with edited value and If-Match header', async () => {
    const refetchMock = vi.fn();
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: SKILL_DATA,
      etag: '"etag-skill-abc"',
      isLoading: false,
      error: null,
      refetch: refetchMock,
    });
    (apiPut as ReturnType<typeof vi.fn>).mockResolvedValue({
      section: 'skill', path: 'db:skill', bytes_written: 10,
    });

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/skill\.yml/i));

    // Wait for data to render
    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_skill_9q2x')).toBeInTheDocument();
    });

    // Edit a skill name field
    const nameInput = screen.getByDisplayValue('FROM_API_skill_9q2x');
    fireEvent.change(nameInput, { target: { value: 'FROM_API_skill_9q2x_edited' } });

    // Click Save
    const saveBtn = screen.getByRole('button', { name: /^save$/i });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith(
        '/api/master/skill',
        expect.arrayContaining([
          expect.objectContaining({ description: 'FROM_API_skill_9q2x_edited' }),
        ]),
        expect.objectContaining({ ifMatch: '"etag-skill-abc"' }),
      );
    });
  });

  it('(c) 412 response: local edits preserved + both action buttons visible', async () => {
    const refetchMock = vi.fn();
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: SKILL_DATA,
      etag: '"etag-skill-abc"',
      isLoading: false,
      error: null,
      refetch: refetchMock,
    });
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(new MockApiError('ETag mismatch', 412));

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_skill_9q2x')).toBeInTheDocument();
    });

    // Edit to make dirty
    const nameInput = screen.getByDisplayValue('FROM_API_skill_9q2x');
    fireEvent.change(nameInput, { target: { value: 'local_edit' } });

    // Click Save → triggers 412
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument();
      expect(screen.getByText(/section changed elsewhere/i)).toBeInTheDocument();
    });

    // Both action buttons visible
    expect(screen.getByRole('button', { name: /discard local \+ refresh/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /overwrite anyway/i })).toBeInTheDocument();

    // Local edits are preserved (the edited value is still in the input)
    expect(screen.getByDisplayValue('local_edit')).toBeInTheDocument();
  });

  it('(c) clicking overwrite-anyway fires second PUT without If-Match guard', async () => {
    const refetchMock = vi.fn();
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: SKILL_DATA,
      etag: '"etag-skill-abc"',
      isLoading: false,
      error: null,
      refetch: refetchMock,
    });
    // First call → 412, second call → success
    (apiPut as ReturnType<typeof vi.fn>)
      .mockRejectedValueOnce(new MockApiError('ETag mismatch', 412))
      .mockResolvedValueOnce({ section: 'skill', path: 'db:skill', bytes_written: 10 });

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_skill_9q2x')).toBeInTheDocument();
    });

    // Make dirty
    fireEvent.change(screen.getByDisplayValue('FROM_API_skill_9q2x'), {
      target: { value: 'overwrite_edit' },
    });

    // First save → 412
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /overwrite anyway/i })).toBeInTheDocument();
    });

    // Click overwrite
    fireEvent.click(screen.getByRole('button', { name: /overwrite anyway/i }));

    await waitFor(() => {
      // Second PUT called with ifMatch: undefined (no If-Match guard)
      expect(apiPut).toHaveBeenCalledTimes(2);
      const secondCall = (apiPut as ReturnType<typeof vi.fn>).mock.calls[1];
      expect(secondCall[2]).toMatchObject({ ifMatch: undefined });
    });
  });

  it('(d) 404 missing_in_db: suggestion code snippet appears', async () => {
    (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
      data: SKILL_DATA,
      etag: '"etag-skill-abc"',
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(new MockApiError('missing_in_db', 404));

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('FROM_API_skill_9q2x')).toBeInTheDocument();
    });

    // Make dirty
    fireEvent.change(screen.getByDisplayValue('FROM_API_skill_9q2x'), {
      target: { value: 'some_edit' },
    });

    // Click Save → triggers 404
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => {
      // Should show the code snippet (fallback suggestion text)
      expect(screen.getByRole('alert')).toBeInTheDocument();
      expect(screen.getByRole('code')).toBeInTheDocument();
    });
  });
});
