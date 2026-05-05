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
class FakeEventSource {
  readyState = 1;
  static CLOSED = 2;
  url: string;
  constructor(url: string) {
    this.url = url;
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

  it('renders an artifact-derived sentinel value when the API returns a fact-check artifact', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      artifacts: [
        {
          run_id: 'run-1',
          specialist: 'apply-factchecker',
          kind: 'fact-check',
          output: {
            claims: [
              {
                claim: 'FROM_API_FIXTURE_unique_claim_xyz',
                source: 'work.yml#FROM_API_SOURCE_qrz',
                ok: true,
              },
            ],
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
      expect(screen.getByText('FROM_API_FIXTURE_unique_claim_xyz')).toBeInTheDocument();
    });
    expect(screen.getByText('work.yml#FROM_API_SOURCE_qrz')).toBeInTheDocument();
  });

  it('renders an artifact-derived sentinel value in the anchors tab', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      artifacts: [
        {
          run_id: 'run-1',
          specialist: 'apply-anchor-scorer',
          kind: 'anchor-check',
          output: {
            preserved: ['FROM_API_anchor_alpha_001', 'FROM_API_anchor_beta_002'],
            dropped: [],
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
      expect(screen.getByText('FROM_API_anchor_alpha_001')).toBeInTheDocument();
    });
    expect(screen.getByText('FROM_API_anchor_beta_002')).toBeInTheDocument();
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
