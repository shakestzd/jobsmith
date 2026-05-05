// config.test.tsx — unit tests for ConfigView
//
// Assertions:
//   GET /api/config hydrates form inputs
//   Validate button calls POST /api/config/validate and renders errors
//   Save button calls PUT /api/config and shows success/422 feedback

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { ConfigView } from './views';

// ── Shared mock config returned by GET /api/config ───────────────────────

const MOCK_CONFIG = {
  master: {
    work_yml: 'assets/content/work.yml',
    skill_yml: 'assets/content/skill.yml',
    education_yml: 'assets/content/education.yml',
    author_yml: 'assets/content/author.yml',
    publication_yml: null,
    award_yml: null,
    projects_yml: null,
  },
  output: {
    applications_dir: 'private/applications',
    job_search_db: 'private/job_search.db',
    jobsmith_db: 'private/jobsmith.db',
    review_db_dir: 'private/.review',
  },
  user: {
    name: 'Jordan Smith',
    email: 'jordan@example.com',
    phone: '+1-555-0100',
    location: 'San Francisco, CA',
    github: 'github.com/jordan',
    linkedin: 'linkedin.com/in/jordan',
  },
  voice: {},
  anchor_thresholds: {},
  cover_letter: {},
  resume: {},
  fit_scorer: {},
  portfolio: {},
  benchmarks: {},
};

// ── Mock the API client module ────────────────────────────────────────────

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = 'JobsmithApiError';
      this.status = status;
    }
  },
}));

import { apiGet, apiPost, apiPut } from '../api/client';

const mockApiGet = vi.mocked(apiGet);
const mockApiPost = vi.mocked(apiPost);
const mockApiPut = vi.mocked(apiPut);

beforeEach(() => {
  vi.clearAllMocks();
  // Default: GET resolves with mock config
  mockApiGet.mockResolvedValue(MOCK_CONFIG);
});

// ── Tests ─────────────────────────────────────────────────────────────────

describe('ConfigView', () => {
  it('shows a loading state while GET is in flight', () => {
    // GET never resolves in this test — stays pending
    mockApiGet.mockReturnValue(new Promise(() => {}));
    render(<ConfigView />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it('GET /api/config hydrates master file inputs', async () => {
    render(<ConfigView />);
    await waitFor(() =>
      expect(screen.getByDisplayValue('assets/content/work.yml')).toBeInTheDocument(),
    );
    expect(screen.getByDisplayValue('assets/content/skill.yml')).toBeInTheDocument();
    expect(screen.getByDisplayValue('assets/content/education.yml')).toBeInTheDocument();
    expect(screen.getByDisplayValue('assets/content/author.yml')).toBeInTheDocument();
  });

  it('GET /api/config hydrates workspace (output) inputs', async () => {
    render(<ConfigView />);
    await waitFor(() =>
      expect(screen.getByDisplayValue('private/applications')).toBeInTheDocument(),
    );
    expect(screen.getByDisplayValue('private/jobsmith.db')).toBeInTheDocument();
  });

  it('validate button calls POST /api/config/validate', async () => {
    mockApiPost.mockResolvedValue({ ok: true, errors: [] });
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /validate/i }));
    });

    expect(mockApiPost).toHaveBeenCalledOnce();
    expect(mockApiPost).toHaveBeenCalledWith('/api/config/validate', expect.any(Object));
  });

  it('shows validation success when POST returns ok:true', async () => {
    mockApiPost.mockResolvedValue({ ok: true, errors: [] });
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /validate/i }));
    });

    await waitFor(() => expect(screen.getByText(/config is valid/i)).toBeInTheDocument());
  });

  it('renders inline errors when POST returns ok:false', async () => {
    mockApiPost.mockResolvedValue({
      ok: false,
      errors: [{ field: 'user.email', message: 'value is not a valid email address' }],
    });
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /validate/i }));
    });

    await waitFor(() =>
      expect(screen.getByText(/value is not a valid email address/i)).toBeInTheDocument(),
    );
  });

  it('save button calls PUT /api/config', async () => {
    mockApiPut.mockResolvedValue(MOCK_CONFIG);
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save/i }));
    });

    expect(mockApiPut).toHaveBeenCalledOnce();
    expect(mockApiPut).toHaveBeenCalledWith('/api/config', expect.any(Object));
  });

  it('shows success message after PUT /api/config resolves', async () => {
    mockApiPut.mockResolvedValue(MOCK_CONFIG);
    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save/i }));
    });

    await waitFor(() => expect(screen.getByText('saved')).toBeInTheDocument());
  });

  it('shows 422 errors inline when PUT returns 422', async () => {
    const { JobsmithApiError } = await import('../api/client');
    const err = new JobsmithApiError(
      JSON.stringify([{ field: 'master.work_yml', message: 'path not found' }]),
      422,
    );
    mockApiPut.mockRejectedValue(err);

    render(<ConfigView />);
    await waitFor(() => screen.getByDisplayValue('assets/content/work.yml'));

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save/i }));
    });

    await waitFor(() => {
      // The error panel header is always a single text node — safe to match exactly.
      expect(screen.getByText('validation errors')).toBeInTheDocument();
      // The field name is rendered in its own <span> — also a single text node.
      expect(screen.getByText('master.work_yml')).toBeInTheDocument();
    });
  });
});
