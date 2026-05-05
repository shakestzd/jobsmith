// application.test.tsx — DOD-pinned tests for the application-detail page
// (feat-83d6cf54, GH#52).
//
// Locks in:
// - The 15-line fabricated event log (`14:02:01` … `14:02:14`) is gone — the
//   stream starts empty and only grows from the real SSE subscription.
// - The hardcoded "Recurly Engineering" / "11m → 2m20s" / "$140k/yr" /
//   "1.2B requests/month" / "p99 < 38ms" / "180 engineers" / "320 services" /
//   "jordan-smith" cover-draft + factcheck + PDF preview content is gone.
// - The hardcoded fact-check rows (5/5 verified against work.yml#…) are gone.
// - The hardcoded anchor list (deploy-pipeline-rebuild, …) is gone.
// - The hardcoded DB writes panel (apply_runs 1 row, bullet_selection 14 rows,
//   …) is gone.
// - When the API returns artifacts, ArtifactsTab / FactCheckTab / AnchorCheckTab
//   render the API values (sentinel test).

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { ApplicationDetail } from './application';

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  postApplication: vi.fn(),
  buildEventsUrl: vi.fn().mockReturnValue('http://localhost/events-noop'),
  redactSensitive: (s: string) => s,
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

import { apiGet } from '../api/client';

// Mock EventSource — jsdom doesn't ship one, and we don't need real SSE here.
// `constructorCalls` lets a test assert whether (and with what URL) the
// component subscribed to the stream — used by the SSE auto-subscribe
// regression test.
const constructorCalls: string[] = [];
class FakeEventSource {
  readyState = 1;
  static CLOSED = 2;
  url: string;
  constructor(url: string) {
    this.url = url;
    constructorCalls.push(url);
  }
  addEventListener() {}
  removeEventListener() {}
  close() {
    this.readyState = 2;
  }
  onerror: ((e: Event) => void) | null = null;
}
(globalThis as { EventSource?: unknown }).EventSource = FakeEventSource;

const BASE_API_DETAIL = {
  slug: 'acme-eng-2026-04',
  run_id: 'run-1',
  phase: 'render',
  status: 'rendered',
  ui_phase: 'rendered',
  started_at: '2026-04-30T12:00:00Z',
  finished_at: '2026-04-30T12:01:30Z',
  role: 'Engineer',
  company: 'Acme',
  artifacts: [],
};

describe('ApplicationDetail anti-regression: no fabricated fixtures (feat-83d6cf54)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(BASE_API_DETAIL);
  });

  it('does not render any of the fabricated event-log timestamps', async () => {
    const { container } = render(
      <ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />,
    );
    await waitFor(() => {
      expect(screen.queryByText(/loading/i)).toBeNull();
    });
    const text = container.textContent ?? '';
    // Each of these timestamps came from the seedEvents fixture array.
    expect(text).not.toContain('14:02:01');
    expect(text).not.toContain('14:02:04');
    expect(text).not.toContain('14:02:14');
  });

  it('does not render any of the fabricated cover-draft / PDF-preview strings', async () => {
    const { container } = render(
      <ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />,
    );
    await waitFor(() => {
      expect(screen.queryByText(/loading/i)).toBeNull();
    });
    const text = container.textContent ?? '';
    expect(text).not.toContain('Recurly Engineering');
    expect(text).not.toContain('11m → 2m20s');
    expect(text).not.toContain('$140k/yr');
    expect(text).not.toContain('1.2B requests/month');
    expect(text).not.toContain('p99 < 38ms');
    expect(text).not.toContain('180 engineers');
    expect(text).not.toContain('320 services');
    // The fake applicant identity from PdfPreview.
    expect(text).not.toMatch(/jordan smith/i);
    expect(text).not.toContain('jordan@smith.dev');
  });

  it('does not render any of the fabricated anchor IDs in the anchors tab', async () => {
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    // Wait for the page to load, then switch to the anchors tab.
    await waitFor(() => {
      expect(screen.getByText('anchors')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('anchors'));
    const anchorIds = [
      'deploy-pipeline-rebuild',
      'artifact-cache-rust',
      'scheduler-migration',
      'live-reload-dev-env',
      'oss-rust-style-guide',
      'team-onboarding-mentorship',
    ];
    for (const id of anchorIds) {
      expect(screen.queryByText(id)).toBeNull();
    }
    // Empty-state message should appear instead.
    expect(screen.getByText(/no anchor-check data yet/i)).toBeInTheDocument();
  });

  it('does not render any of the fabricated DB-writes row counts', async () => {
    const { container } = render(
      <ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />,
    );
    await waitFor(() => {
      expect(screen.queryByText(/loading/i)).toBeNull();
    });
    const text = container.textContent ?? '';
    expect(text).not.toMatch(/apply_runs\s+1 row/);
    expect(text).not.toMatch(/bullet_selection\s+14 rows/);
    expect(text).not.toMatch(/cover_draft\s+1 row/);
  });

  it('does not render fabricated factcheck claims when no fact-check artifact is present', async () => {
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('factcheck')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('factcheck'));
    expect(screen.getByText(/no fact-check data yet/i)).toBeInTheDocument();
    // None of the fake claims should leak in.
    expect(screen.queryByText(/work\.yml#deploy-pipeline/)).toBeNull();
    expect(screen.queryByText(/work\.yml#artifact-cache/)).toBeNull();
    expect(screen.queryByText(/42% page volume reduction/)).toBeNull();
  });
});

describe('ApplicationDetail positive: API artifacts surface in DOM (feat-83d6cf54)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders fact-check rows from the real FactCheckResult schema (verified_claims + failed_claims)', async () => {
    // Real artifact shape from src/jobsmith/factcheck.py:
    //   { passed, verified_claims: [{claim, kind, verified, source_file?}], failed_claims: [str] }
    // Roborev job 945 anti-regression.
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      artifacts: [
        {
          run_id: 'run-1',
          specialist: 'apply-factchecker',
          kind: 'fact-check',
          output: {
            passed: false,
            verified_claims: [
              {
                claim: 'FROM_API_FIXTURE_verified_claim_xyz',
                kind: 'money',
                verified: true,
                source_file: 'work.yml#FROM_API_SOURCE_qrz',
              },
            ],
            failed_claims: ['FROM_API_FIXTURE_failed_claim_abc'],
          },
          finished_at: '2026-04-30T12:01:00Z',
          transcript_ref: null,
        },
      ],
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('factcheck')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('factcheck'));
    await waitFor(() => {
      expect(screen.getByText('FROM_API_FIXTURE_verified_claim_xyz')).toBeInTheDocument();
    });
    // Source comes from `source_file` (real shape), not `source` (legacy).
    expect(screen.getByText('work.yml#FROM_API_SOURCE_qrz')).toBeInTheDocument();
    // Failed claim must also surface as a row, marked unverified.
    expect(screen.getByText('FROM_API_FIXTURE_failed_claim_abc')).toBeInTheDocument();
    // `passed: false` should put the summary badge in the failed state.
    // The badge text is "1/2 verified · failed" — match it specifically.
    expect(screen.getByText(/1\/2 verified · failed/)).toBeInTheDocument();
  });

  it('renders anchor-check from the real GuardResult schema (kept + dropped_without_reason + dropped_with_reason)', async () => {
    // Real artifact shape from src/jobsmith/guard.py GuardResult:
    //   { exit_code, anchor_bullets, kept, dropped_without_reason, dropped_with_reason }
    // Roborev job 945 anti-regression.
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      artifacts: [
        {
          run_id: 'run-1',
          specialist: 'apply-anchor-scorer',
          kind: 'anchor-check',
          output: {
            exit_code: 1,
            anchor_bullets: [
              { bullet_id: 'bul-001', text: 'FROM_API_BULLET_alpha' },
              { bullet_id: 'bul-002', text: 'FROM_API_BULLET_beta' },
              { bullet_id: 'bul-003', text: 'FROM_API_BULLET_gamma' },
            ],
            kept: [{ bullet_id: 'bul-001', text: 'FROM_API_BULLET_alpha' }],
            dropped_without_reason: [
              { bullet_id: 'bul-002', text: 'FROM_API_BULLET_beta' },
            ],
            dropped_with_reason: [
              [{ bullet_id: 'bul-003', text: 'FROM_API_BULLET_gamma' }, 'FROM_API_REASON_delta'],
            ],
            message: 'FROM_API_GUARD_MESSAGE_epsilon',
          },
          finished_at: '2026-04-30T12:00:30Z',
          transcript_ref: null,
        },
      ],
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('anchors')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('anchors'));
    await waitFor(() => {
      expect(screen.getByText('FROM_API_BULLET_alpha')).toBeInTheDocument();
    });
    expect(screen.getByText('FROM_API_BULLET_beta')).toBeInTheDocument();
    expect(screen.getByText('FROM_API_BULLET_gamma')).toBeInTheDocument();
    expect(screen.getByText('FROM_API_REASON_delta')).toBeInTheDocument();
    // Top-level message surfaces (esp. on failure path).
    expect(screen.getByText('FROM_API_GUARD_MESSAGE_epsilon')).toBeInTheDocument();
    // exit_code !== 0 OR dropped_without_reason >0 → "failed" badge.
    // Badge text: "1 / 3 preserved · failed".
    expect(screen.getByText(/1 \/ 3 preserved · failed/)).toBeInTheDocument();
    // The "dropped without reason" header surfaces with count, in danger color.
    expect(screen.getByText(/dropped without reason \(1\)/i)).toBeInTheDocument();
  });

  it('renders an artifact-derived sentinel value in the artifacts tab', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      artifacts: [
        {
          run_id: 'run-1',
          specialist: 'apply-jd-parser',
          kind: 'jd-parsed',
          output: {
            company: 'FROM_API_FIXTURE_company_omega',
            position: 'FROM_API_FIXTURE_role_omega',
          },
          finished_at: '2026-04-30T12:00:15Z',
          transcript_ref: null,
        },
      ],
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('artifacts')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('artifacts'));
    await waitFor(() => {
      // The artifact's JSON is rendered as a <pre> block.
      expect(
        screen.getByText(/FROM_API_FIXTURE_company_omega/),
      ).toBeInTheDocument();
    });
  });

  it('shows empty-state message in artifacts tab when no artifacts are present', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      artifacts: [],
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('artifacts')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('artifacts'));
    expect(screen.getByText(/no artifacts yet/i)).toBeInTheDocument();
  });
});

describe('ApplicationDetail SSE auto-subscribe (roborev job 945)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    constructorCalls.length = 0;
  });

  it('auto-subscribes to the SSE stream when opening a slug whose status is "running"', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'running',
      ui_phase: 'running',
      finished_at: null,
    });
    render(<ApplicationDetail slug="still-running" back={() => {}} />);
    await waitFor(() => {
      expect(constructorCalls.length).toBeGreaterThan(0);
    });
    // The buildEventsUrl mock returns a known URL — confirm EventSource was
    // constructed with it. Without the auto-subscribe effect, opening a
    // running slug would never call EventSource and the log would freeze.
    expect(constructorCalls[0]).toBe('http://localhost/events-noop');
  });

  it('does NOT auto-subscribe when opening a completed (status="rendered") slug', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'rendered',
      ui_phase: 'rendered',
    });
    render(<ApplicationDetail slug="finished" back={() => {}} />);
    // Wait long enough that any auto-subscribe would have fired.
    await waitFor(() => {
      expect(screen.getByText('finished')).toBeInTheDocument();
    });
    expect(constructorCalls.length).toBe(0);
  });
});
