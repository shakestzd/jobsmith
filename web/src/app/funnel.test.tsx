// funnel.test.tsx — component tests for FunnelView.
//
// Coverage:
//  - renders loading state
//  - renders stage counts (sourced, queued, promoted, interview, offer)
//  - renders conversion percentages between adjacent stages
//  - renders per-source yield table
//  - window filter buttons render and are clickable
//  - "no data" message when all counts are zero

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { FunnelView } from './funnel';

vi.mock('../api/client', () => ({
  getFunnel: vi.fn(),
}));

import { getFunnel } from '../api/client';
import type { FunnelResponse } from '../api/types';

const FUNNEL_FIXTURE: FunnelResponse = {
  window: 30,
  stages: {
    sourced: 10,
    queued: 4,
    promoted: 2,
    interview: 1,
    offer: 0,
  },
  conversions: {
    sourced_to_queued: 0.4,
    queued_to_promoted: 0.3333,
    promoted_to_interview: 0.5,
    interview_to_offer: null,
  },
  per_source: [
    { source: 'greenhouse/stripe', postings: 6, applied: 2, interview: 1, offer: 0 },
    { source: 'email/linkedin', postings: 4, applied: 0, interview: 0, offer: 0 },
  ],
};

const EMPTY_FUNNEL: FunnelResponse = {
  window: 30,
  stages: { sourced: 0, queued: 0, promoted: 0, interview: 0, offer: 0 },
  conversions: {
    sourced_to_queued: null,
    queued_to_promoted: null,
    promoted_to_interview: null,
    interview_to_offer: null,
  },
  per_source: [],
};

function setupMock(data: FunnelResponse = FUNNEL_FIXTURE) {
  (getFunnel as ReturnType<typeof vi.fn>).mockResolvedValue(data);
}

describe('FunnelView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupMock();
  });

  it('renders loading state initially', async () => {
    // Make getFunnel never resolve during this test
    (getFunnel as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    render(<FunnelView />);
    expect(screen.getByText(/loading funnel/i)).toBeTruthy();
  });

  it('renders stage counts after load', async () => {
    await act(async () => { render(<FunnelView />); });
    expect(screen.getByText('10')).toBeTruthy(); // sourced (unique)
    // queued=4 may also appear in per-source table, use getAllByText
    expect(screen.getAllByText('4').length).toBeGreaterThan(0);
    // "2" appears multiple times (promoted stage + applied column)
    expect(screen.getAllByText('2').length).toBeGreaterThan(0);
    // interview=1
    expect(screen.getAllByText('1').length).toBeGreaterThan(0);
  });

  it('renders stage labels', async () => {
    await act(async () => { render(<FunnelView />); });
    expect(screen.getByText('sourced')).toBeTruthy();
    expect(screen.getByText('queued')).toBeTruthy();
    expect(screen.getByText('promoted')).toBeTruthy();
    expect(screen.getByText('interview')).toBeTruthy();
    expect(screen.getByText('offer')).toBeTruthy();
  });

  it('renders conversion percentages', async () => {
    await act(async () => { render(<FunnelView />); });
    // 0.4 → "40%", 0.5 → "50%"
    expect(screen.getByText('40%')).toBeTruthy();
    expect(screen.getByText('50%')).toBeTruthy();
  });

  it('renders per-source table header', async () => {
    await act(async () => { render(<FunnelView />); });
    // "Source" table header (may appear with stage label "sourced" too — use getAllByText)
    expect(screen.getAllByText(/source/i).length).toBeGreaterThan(0);
  });

  it('renders per-source rows', async () => {
    await act(async () => { render(<FunnelView />); });
    expect(screen.getByText('greenhouse/stripe')).toBeTruthy();
    expect(screen.getByText('email/linkedin')).toBeTruthy();
  });

  it('renders window filter buttons', async () => {
    await act(async () => { render(<FunnelView />); });
    expect(screen.getByText('7d')).toBeTruthy();
    expect(screen.getByText('30d')).toBeTruthy();
    expect(screen.getByText('90d')).toBeTruthy();
    expect(screen.getByText('all')).toBeTruthy();
  });

  it('clicking window button calls getFunnel with correct window', async () => {
    await act(async () => { render(<FunnelView />); });
    const btn7d = screen.getByText('7d');
    await act(async () => { fireEvent.click(btn7d); });
    expect(getFunnel).toHaveBeenCalledWith(7);
  });

  it('shows "no data" message when all zero', async () => {
    setupMock(EMPTY_FUNNEL);
    await act(async () => { render(<FunnelView />); });
    expect(screen.getByText(/no postings/i)).toBeTruthy();
  });

  it('renders "funnel" heading', async () => {
    await act(async () => { render(<FunnelView />); });
    expect(screen.getByText(/funnel/i)).toBeTruthy();
  });
});
