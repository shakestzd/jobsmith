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
//  - coverage column: renders value for scored row, em dash for null with tooltip
//  - coverage column: sorts with nulls last in both directions
//  - min-coverage filter: excludes below-threshold rows, keeps null-coverage rows
//  - gap badges: render from gap_hits on scored rows, on null-coverage rows too
//  - apply button: remains enabled on gap-flagged row

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
import type { PostingRow } from '../api/types';

const POSTING_FIXTURE: PostingRow = {
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
  coverage_score: null,
  uncovered: null,
  gap_hits: null,
};

const POSTING_WITH_COVERAGE: PostingRow = {
  ...POSTING_FIXTURE,
  id: 2,
  title: 'Data Engineer',
  company: 'Databricks',
  coverage_score: 75,
  uncovered: ['dbt'],
  gap_hits: [{ gap: 'DBT', term: 'dbt' }],
};

const POSTING_NULL_COVERAGE_WITH_GAPS: PostingRow = {
  ...POSTING_FIXTURE,
  id: 3,
  title: 'ML Engineer',
  company: 'OpenAI',
  coverage_score: null,
  uncovered: null,
  gap_hits: [{ gap: 'Spark', term: 'spark' }],
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

  // ── Coverage column tests ──────────────────────────────────────────────────

  it('renders coverage column header', () => {
    setupMockHook([POSTING_WITH_COVERAGE]);
    render(<PostingsView />);
    expect(screen.getByText(/coverage/i)).toBeTruthy();
  });

  it('renders numeric coverage value for scored row', () => {
    setupMockHook([POSTING_WITH_COVERAGE]);
    render(<PostingsView />);
    // coverage_score=75 → "75"
    expect(screen.getByText('75')).toBeTruthy();
  });

  it('renders em dash for null coverage with not-rescored tooltip', () => {
    setupMockHook([POSTING_FIXTURE]);
    render(<PostingsView />);
    // Should find an element with title "not rescored" that contains em dash
    const emDashEl = screen.getByTitle('not rescored');
    expect(emDashEl).toBeTruthy();
    expect(emDashEl.textContent).toBe('—');
  });

  it('sorts coverage column: nulls last when ascending', () => {
    const low: PostingRow = { ...POSTING_FIXTURE, id: 10, title: 'Low Score', coverage_score: 30, gap_hits: null, uncovered: null };
    const high: PostingRow = { ...POSTING_FIXTURE, id: 11, title: 'High Score', coverage_score: 90, gap_hits: null, uncovered: null };
    const nullCov: PostingRow = { ...POSTING_FIXTURE, id: 12, title: 'Null Coverage', coverage_score: null, gap_hits: null, uncovered: null };
    setupMockHook([low, high, nullCov]);
    render(<PostingsView />);
    const coverageHeader = screen.getByRole('columnheader', { name: /^coverage/i });
    fireEvent.click(coverageHeader);
    const rows = screen.getAllByRole('row');
    // Skip header row; find data rows by title text order
    const titles = rows.slice(1).map((r) => r.textContent ?? '').filter((t) => t.includes('Score') || t.includes('Coverage'));
    expect(titles[titles.length - 1]).toContain('Null Coverage');
  });

  it('sorts coverage column: nulls last when descending', () => {
    const low: PostingRow = { ...POSTING_FIXTURE, id: 10, title: 'Low Score', coverage_score: 30, gap_hits: null, uncovered: null };
    const high: PostingRow = { ...POSTING_FIXTURE, id: 11, title: 'High Score', coverage_score: 90, gap_hits: null, uncovered: null };
    const nullCov: PostingRow = { ...POSTING_FIXTURE, id: 12, title: 'Null Coverage', coverage_score: null, gap_hits: null, uncovered: null };
    setupMockHook([low, high, nullCov]);
    render(<PostingsView />);
    const coverageHeader = screen.getByRole('columnheader', { name: /^coverage/i });
    fireEvent.click(coverageHeader); // asc
    fireEvent.click(coverageHeader); // desc
    const rows = screen.getAllByRole('row');
    const titles = rows.slice(1).map((r) => r.textContent ?? '').filter((t) => t.includes('Score') || t.includes('Coverage'));
    expect(titles[titles.length - 1]).toContain('Null Coverage');
  });

  // ── Min-coverage filter tests ──────────────────────────────────────────────

  it('min-coverage filter excludes below-threshold rows', () => {
    const low: PostingRow = { ...POSTING_FIXTURE, id: 10, title: 'Low Coverage Row', coverage_score: 30, gap_hits: null, uncovered: null };
    const high: PostingRow = { ...POSTING_FIXTURE, id: 11, title: 'High Coverage Row', coverage_score: 90, gap_hits: null, uncovered: null };
    setupMockHook([low, high]);
    render(<PostingsView />);
    const minCovInput = screen.getByPlaceholderText(/min coverage/i);
    fireEvent.change(minCovInput, { target: { value: '50' } });
    expect(screen.queryByText('Low Coverage Row')).toBeNull();
    expect(screen.getByText('High Coverage Row')).toBeTruthy();
  });

  it('min-coverage filter passes through null-coverage rows', () => {
    const nullCov: PostingRow = { ...POSTING_FIXTURE, id: 12, title: 'Null Coverage Row', coverage_score: null, gap_hits: null, uncovered: null };
    const low: PostingRow = { ...POSTING_FIXTURE, id: 10, title: 'Low Coverage Row', coverage_score: 20, gap_hits: null, uncovered: null };
    setupMockHook([nullCov, low]);
    render(<PostingsView />);
    const minCovInput = screen.getByPlaceholderText(/min coverage/i);
    fireEvent.change(minCovInput, { target: { value: '50' } });
    // null-coverage row always passes through
    expect(screen.getByText('Null Coverage Row')).toBeTruthy();
    // low-coverage row excluded
    expect(screen.queryByText('Low Coverage Row')).toBeNull();
  });

  // ── Gap badge tests ────────────────────────────────────────────────────────

  it('renders gap badge from gap_hits on scored row', () => {
    setupMockHook([POSTING_WITH_COVERAGE]);
    render(<PostingsView />);
    // Badge should show gap label "DBT"
    expect(screen.getByText('DBT')).toBeTruthy();
  });

  it('gap badge has matched term in tooltip', () => {
    setupMockHook([POSTING_WITH_COVERAGE]);
    render(<PostingsView />);
    const badge = screen.getByTitle('matched: dbt');
    expect(badge).toBeTruthy();
    expect(badge.textContent).toBe('DBT');
  });

  it('renders gap badge on null-coverage row', () => {
    setupMockHook([POSTING_NULL_COVERAGE_WITH_GAPS]);
    render(<PostingsView />);
    expect(screen.getByText('Spark')).toBeTruthy();
  });

  // ── Apply button not disabled by coverage ─────────────────────────────────

  it('apply button is enabled on gap-flagged row', () => {
    setupMockHook([POSTING_WITH_COVERAGE]);
    render(<PostingsView />);
    const applyBtn = screen.getByText('apply');
    expect((applyBtn as HTMLButtonElement).disabled).toBe(false);
  });
});
