// SourcingHealthBanner.test.tsx — unit tests for SourcingHealthBanner
//
// Assertions:
//   renders nothing when state is 'ok'
//   renders "FAILED" banner with error message for 'failed' state
//   renders "DEGRADED" banner with source list for 'degraded' state
//   renders "STALE" banner with age for 'stale' state
//   renders "NO RUNS YET" banner for 'no_runs' state
//   renders "UNKNOWN" banner for 'unknown' state
//   shows loading state as nothing (non-blocking)
//   shows error state as nothing (non-blocking)

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SourcingHealthBanner } from './SourcingHealthBanner';

vi.mock('../api/hooks', () => ({
  useRunHealth: vi.fn(),
}));

import { useRunHealth } from '../api/hooks';

describe('SourcingHealthBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing when state is ok', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { state: 'ok' },
      isLoading: false,
      error: null,
    });

    const { container } = render(<SourcingHealthBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing while loading', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined,
      isLoading: true,
      error: null,
    });

    const { container } = render(<SourcingHealthBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing on error', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new Error('Network error'),
    });

    const { container } = render(<SourcingHealthBanner />);
    expect(container.firstChild).toBeNull();
  });

  it('renders failed state with error message', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        state: 'failed',
        error: 'Connection timeout to LinkedIn',
      },
      isLoading: false,
      error: null,
    });

    render(<SourcingHealthBanner />);
    expect(screen.getByText('Last sourcing run FAILED')).toBeInTheDocument();
    expect(screen.getByText('Error: Connection timeout to LinkedIn')).toBeInTheDocument();
  });

  it('renders failed state without error message', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        state: 'failed',
        error: null,
      },
      isLoading: false,
      error: null,
    });

    render(<SourcingHealthBanner />);
    expect(screen.getByText('Last sourcing run FAILED')).toBeInTheDocument();
  });

  it('renders degraded state with source list', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        state: 'degraded',
        degraded_sources: ['linkedin', 'indeed'],
      },
      isLoading: false,
      error: null,
    });

    render(<SourcingHealthBanner />);
    expect(screen.getByText('Last sourcing run DEGRADED')).toBeInTheDocument();
    expect(screen.getByText('Sources with errors: linkedin, indeed')).toBeInTheDocument();
  });

  it('renders degraded state without sources', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        state: 'degraded',
        degraded_sources: null,
      },
      isLoading: false,
      error: null,
    });

    render(<SourcingHealthBanner />);
    expect(screen.getByText('Last sourcing run DEGRADED')).toBeInTheDocument();
  });

  it('renders stale state with age', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        state: 'stale',
        age_hours: 30.5,
      },
      isLoading: false,
      error: null,
    });

    render(<SourcingHealthBanner />);
    expect(screen.getByText('Last sourcing run is STALE')).toBeInTheDocument();
    expect(screen.getByText('Last successful run was 30.5 hours ago (>25h threshold).')).toBeInTheDocument();
  });

  it('renders no_runs state', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        state: 'no_runs',
      },
      isLoading: false,
      error: null,
    });

    render(<SourcingHealthBanner />);
    expect(screen.getByText('No sourcing runs yet')).toBeInTheDocument();
    expect(screen.getByText('The sourcing pipeline has not completed a run. Check back soon.')).toBeInTheDocument();
  });

  it('renders unknown state', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        state: 'unknown',
      },
      isLoading: false,
      error: null,
    });

    render(<SourcingHealthBanner />);
    expect(screen.getByText('Unable to check sourcing health')).toBeInTheDocument();
    expect(screen.getByText('Could not determine the status of the sourcing pipeline.')).toBeInTheDocument();
  });

  it('applies custom className', () => {
    (useRunHealth as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        state: 'failed',
      },
      isLoading: false,
      error: null,
    });

    const { container } = render(<SourcingHealthBanner className="custom-class" />);
    const banner = container.querySelector('.custom-class');
    expect(banner).toBeInTheDocument();
  });
});
