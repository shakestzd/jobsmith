// postings.test.tsx — component tests for PostingsView.
//
// Coverage:
//  - renders loading state
//  - renders empty state when no postings
//  - renders postings list with title/company/score/age
//  - dismiss button calls setPostingStatus
//  - promote button calls promotePosting and shows success message
//  - promote with jd_fetch_failed shows warning in success message
//  - filter tabs visible

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { PostingsView } from './postings';

// Mock the API hooks and client
vi.mock('../api/hooks', () => ({
  usePostings: vi.fn(),
  useRunHealth: vi.fn(),
}));
vi.mock('../api/client', () => ({
  setPostingStatus: vi.fn(),
  promotePosting: vi.fn(),
  notifyDataChanged: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error {
    status: number;
    constructor(message: string, status: number) {
      super(message);
      this.name = 'JobsmithApiError';
      this.status = status;
    }
  },
}));
vi.mock('./SourcingHealthBanner', () => ({
  default: () => <div data-testid="sourcing-health-banner" />,
}));

import { usePostings } from '../api/hooks';
import { setPostingStatus, promotePosting } from '../api/client';

const POSTING_FIXTURE = {
  id: 1,
  source: 'greenhouse/stripe',
  title: 'Senior Engineer',
  company: 'Stripe',
  location: 'Remote',
  specialty: 'backend',
  llm_score: 0.9,
  fast_score: 0.8,
  rationale: 'Strong Python match',
  status: 'sourced',
  dedup_key: 'key-a',
  first_seen_at: '2026-06-01T10:00:00Z',
  last_seen_at: '2026-06-01T10:00:00Z',
  url: 'https://stripe.com/jobs/1',
};

function setupMockHook(postings = [POSTING_FIXTURE]) {
  (usePostings as ReturnType<typeof vi.fn>).mockReturnValue({
    data: postings,
    isLoading: false,
    error: null,
  });
}

describe('PostingsView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: return mock data for all hook calls
    setupMockHook();
  });

  it('renders loading state', () => {
    (usePostings as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });
    render(<PostingsView />);
    expect(screen.getByText(/loading postings/i)).toBeTruthy();
  });

  it('renders empty state when no postings', () => {
    (usePostings as ReturnType<typeof vi.fn>).mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    });
    render(<PostingsView />);
    expect(screen.getByText(/no postings match/i)).toBeTruthy();
  });

  it('renders posting title and company', () => {
    setupMockHook();
    render(<PostingsView />);
    expect(screen.getByText('Senior Engineer')).toBeTruthy();
    expect(screen.getByText('Stripe')).toBeTruthy();
  });

  it('renders triage score as percentage', () => {
    setupMockHook();
    render(<PostingsView />);
    // llm_score=0.9 → "90"
    expect(screen.getByText('90')).toBeTruthy();
  });

  it('renders filter tabs', () => {
    setupMockHook();
    render(<PostingsView />);
    expect(screen.getByText('sourced')).toBeTruthy();
    expect(screen.getByText('queued')).toBeTruthy();
    expect(screen.getByText('dismissed')).toBeTruthy();
  });

  it('renders "postings inbox" heading', () => {
    setupMockHook();
    render(<PostingsView />);
    expect(screen.getByText(/postings inbox/i)).toBeTruthy();
  });

  it('dismiss button calls setPostingStatus with dismissed', async () => {
    setupMockHook();
    (setPostingStatus as ReturnType<typeof vi.fn>).mockResolvedValue({ ...POSTING_FIXTURE, status: 'dismissed' });
    render(<PostingsView />);

    const dismissBtn = screen.getByTitle('dismiss');
    await act(async () => { fireEvent.click(dismissBtn); });
    expect(setPostingStatus).toHaveBeenCalledWith(1, 'dismissed');
  });

  it('promote button calls promotePosting and shows success', async () => {
    setupMockHook();
    (promotePosting as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'run-abc123',
      slug: 'stripe-senior-eng',
      jd_fetch_failed: false,
    });
    render(<PostingsView />);

    const applyBtn = screen.getByText('apply');
    await act(async () => { fireEvent.click(applyBtn); });
    expect(promotePosting).toHaveBeenCalledWith(1);
    await waitFor(() => {
      expect(screen.getByText(/Application started/i)).toBeTruthy();
    });
  });

  it('promote with jd_fetch_failed shows warning in success message', async () => {
    setupMockHook();
    (promotePosting as ReturnType<typeof vi.fn>).mockResolvedValue({
      run_id: 'run-abc123',
      slug: 'stripe-senior-eng',
      jd_fetch_failed: true,
    });
    render(<PostingsView />);

    const applyBtn = screen.getByText('apply');
    await act(async () => { fireEvent.click(applyBtn); });
    await waitFor(() => {
      expect(screen.getByText(/JD fetch failed/i)).toBeTruthy();
    });
  });

  it('includes SourcingHealthBanner at the top', () => {
    setupMockHook();
    render(<PostingsView />);
    expect(screen.getByTestId('sourcing-health-banner')).toBeInTheDocument();
  });
});
