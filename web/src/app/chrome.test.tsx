// chrome.test.tsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { Sidebar } from './chrome';
import type { ApplicationRow } from '../api/types';

// Mock the hooks module so Sidebar can use real application data without HTTP
vi.mock('../api/hooks', () => ({
  useApplications: vi.fn(),
}));

// Mock the api/client module so the dynamic import inside usePostingsBadge
// never hits the network.
const mockGetPostings = vi.fn().mockResolvedValue([]);
vi.mock('../api/client', () => ({
  getPostings: (...args: unknown[]) => mockGetPostings(...args),
}));

import { useApplications } from '../api/hooks';
const mockUseApplications = vi.mocked(useApplications);

function makeApp(overrides: Partial<ApplicationRow>): ApplicationRow {
  return {
    slug: 'test-slug',
    run_id: 'run-1',
    phase: 'render',
    status: 'running',
    ui_phase: 'running',
    started_at: null,
    finished_at: null,
    role: 'Engineer',
    company: 'Acme',
    ...overrides,
  };
}

describe('Sidebar', () => {
  beforeEach(() => {
    // Default: no data yet (loading state)
    mockUseApplications.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });
  });

  function renderSidebar(openNew = vi.fn()) {
    return render(
      <Sidebar view="dashboard" open={true} setView={vi.fn()} openNew={openNew} />,
    );
  }

  it('does not render an "Outputs" section header', () => {
    renderSidebar();
    // The nav-section label "Outputs" was non-clickable and misleading — it must
    // not appear in the sidebar after feat-94f8bec1.
    expect(screen.queryByText('Outputs')).toBeNull();
  });

  it('still renders the Listings site and Feedback nav items', () => {
    renderSidebar();
    expect(screen.getByText('Listings site')).toBeInTheDocument();
    expect(screen.getByText('Feedback')).toBeInTheDocument();
  });

  it('new application button calls openNew, not a hardcoded route', () => {
    const openNew = vi.fn();
    renderSidebar(openNew);
    // The button text contains "new application" (with the ⌘N kbd hint alongside)
    const btn = screen.getByRole('button', { name: /new application/i });
    fireEvent.click(btn);
    expect(openNew).toHaveBeenCalledTimes(1);
  });

  it('shows no counts while data is loading', () => {
    mockUseApplications.mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });
    renderSidebar();
    // No nav-count spans should be present when data is not yet loaded
    const countSpans = document.querySelectorAll('.nav-count');
    expect(countSpans).toHaveLength(0);
  });

  it('counts dashboard = total rows, running = ui_phase running', () => {
    const apps = [
      makeApp({ slug: 'a1', ui_phase: 'running' }),
      makeApp({ slug: 'a2', ui_phase: 'running' }),
      makeApp({ slug: 'a3', ui_phase: 'rendered' }),
      makeApp({ slug: 'a4', ui_phase: 'failed' }),
    ];
    mockUseApplications.mockReturnValue({
      data: apps,
      isLoading: false,
      error: null,
    });
    renderSidebar();

    const allCounts = document.querySelectorAll('.nav-count');
    const countTexts = Array.from(allCounts).map((el) => el.textContent);
    expect(countTexts).toContain('4'); // dashboard total
    expect(countTexts).toContain('2'); // running count
  });

  it('review count is 0 — API does not currently emit ui_phase=review', () => {
    const apps = [
      makeApp({ slug: 'a1', ui_phase: 'running' }),
      makeApp({ slug: 'a2', ui_phase: 'rendered' }),
    ];
    mockUseApplications.mockReturnValue({
      data: apps,
      isLoading: false,
      error: null,
    });
    renderSidebar();

    // Find the "Needs review" nav item and verify its count span shows 0
    const navItems = document.querySelectorAll('.nav-item');
    const reviewItem = Array.from(navItems).find(
      (el) => el.textContent?.includes('Needs review'),
    );
    expect(reviewItem).toBeTruthy();
    const countSpan = reviewItem?.querySelector('.nav-count');
    expect(countSpan?.textContent).toBe('0');
  });
});

// ── usePostingsBadge — refetches on jobsmith:data-changed (finding #4) ──────

describe('usePostingsBadge via Sidebar', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetPostings.mockResolvedValue([]);
    // Use a stable applications response so Sidebar renders without issues
    vi.mocked(useApplications).mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    });
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('calls getPostings again when jobsmith:data-changed fires', async () => {
    // Reset call count
    mockGetPostings.mockClear();
    mockGetPostings.mockResolvedValue([]);

    render(<Sidebar view="dashboard" open={true} setView={vi.fn()} openNew={vi.fn()} />);

    // Wait for the initial fetch triggered by mount (version=0 effect)
    await act(async () => {
      await Promise.resolve();
    });
    const callsAfterMount = mockGetPostings.mock.calls.length;
    expect(callsAfterMount).toBeGreaterThanOrEqual(1);

    // Fire the data-changed event
    await act(async () => {
      window.dispatchEvent(new Event('jobsmith:data-changed'));
      await Promise.resolve();
    });

    // The hook should have scheduled another fetch
    expect(mockGetPostings.mock.calls.length).toBeGreaterThan(callsAfterMount);
  });
});
