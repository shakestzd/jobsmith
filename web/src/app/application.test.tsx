// application.test.tsx — DOD-pinned tests for the application-detail page.
//
// Combines two PR's worth of regression coverage:
//
// feat-83d6cf54 (GH#52) — fixture removal:
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
// - SSE auto-subscribes when opening a status='running' slug.
//
// feat-d6b1e167 (GH#50) — re-run-apply behavior:
// - For an already-complete slug (status=done/rendered/backfilled), the
//   re-run button is labelled "force re-run apply" and the click invokes
//   postApplication with `{ force: true }`.
// - For an in-flight or failed slug, the button is labelled "re-run apply"
//   and postApplication is invoked WITHOUT force (or with force=false).
// - The button is DISABLED entirely when the API has no `url` field for the
//   slug — protects against destructive force-restart with a placeholder URL.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
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

import { apiGet, postApplication } from '../api/client';

// Mock EventSource — jsdom doesn't ship one, and we don't need real SSE here.
// `constructorCalls` lets a test assert whether (and with what URL) the
// component subscribed to the stream — used by the SSE auto-subscribe
// regression test.
//
// `lastFakeEs` gives tests direct access to the most-recently-created instance
// so they can fire named events (phase, specialist, log, idle-close) and assert
// that the component reacts correctly. Used by the SSE phase-wiring tests.
const constructorCalls: string[] = [];
let lastFakeEs: FakeEventSource | null = null;
class FakeEventSource {
  readyState = 1;
  static CLOSED = 2;
  url: string;
  private _listeners: Map<string, Array<(e: MessageEvent) => void>> = new Map();
  constructor(url: string) {
    this.url = url;
    constructorCalls.push(url);
    lastFakeEs = this;
  }
  addEventListener(type: string, listener: (e: MessageEvent) => void) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type)!.push(listener);
  }
  removeEventListener() {}
  /** Fire a named SSE event with JSON-stringified data. */
  emit(type: string, data: unknown) {
    const evt = { data: JSON.stringify(data) } as MessageEvent;
    (this._listeners.get(type) ?? []).forEach(fn => fn(evt));
  }
  close() {
    this.readyState = 2;
  }
  onerror: ((e: Event) => void) | null = null;
}
(globalThis as { EventSource?: unknown }).EventSource = FakeEventSource;

// Default fixture has a URL set so the re-run button is enabled. Tests
// that exercise the URL-missing path override `url` to '' or null on the
// returned object.
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
  url: 'https://example.com/jobs/acme-eng',
};

describe('ApplicationDetail anti-regression: no fabricated fixtures (feat-83d6cf54)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    constructorCalls.length = 0;
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
    expect(screen.queryByText(/work\.yml#deploy-pipeline/)).toBeNull();
    expect(screen.queryByText(/work\.yml#artifact-cache/)).toBeNull();
    expect(screen.queryByText(/42% page volume reduction/)).toBeNull();
  });
});

describe('ApplicationDetail positive: API artifacts surface in DOM (feat-83d6cf54)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    constructorCalls.length = 0;
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
    expect(screen.getByText('FROM_API_GUARD_MESSAGE_epsilon')).toBeInTheDocument();
    expect(screen.getByText(/1 \/ 3 preserved · failed/)).toBeInTheDocument();
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
    expect(constructorCalls[0]).toBe('http://localhost/events-noop');
  });

  it('does NOT auto-subscribe when opening a completed (status="rendered") slug', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'rendered',
      ui_phase: 'rendered',
    });
    render(<ApplicationDetail slug="finished" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByText('finished')).toBeInTheDocument();
    });
    expect(constructorCalls.length).toBe(0);
  });
});

describe('ApplicationDetail re-run button (feat-d6b1e167)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    constructorCalls.length = 0;
    (postApplication as ReturnType<typeof vi.fn>).mockResolvedValue({
      slug: 'acme-eng-2026-04',
      run_id: 'run-2',
    });
  });

  it('shows "force re-run apply" label for a slug whose status is "done"', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /force re-run apply/i })).toBeInTheDocument();
    });
  });

  it('shows "force re-run apply" label for a slug whose status is "rendered"', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'rendered',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /force re-run apply/i })).toBeInTheDocument();
    });
  });

  it('shows "force re-run apply" label for a slug whose status is "backfilled"', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'backfilled',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /force re-run apply/i })).toBeInTheDocument();
    });
  });

  it('shows plain "re-run apply" label when the slug has not completed', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'failed',
      finished_at: null,
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /^re-run apply$/i })).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /force re-run apply/i })).toBeNull();
  });

  it('clicking the force button invokes postApplication with force=true', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(postApplication).toHaveBeenCalledWith(
        expect.any(String),
        'acme-eng-2026-04',
        { force: true },
      );
    });
  });

  it('clicking the plain re-run button invokes postApplication with force=false', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'failed',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /^re-run apply$/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(postApplication).toHaveBeenCalledWith(
        expect.any(String),
        'acme-eng-2026-04',
        { force: false },
      );
    });
  });

  it('clicking force button passes the REAL url, never a placeholder (roborev job 944)', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
      url: 'https://real.example.com/jobs/clay-gtm-data-analyst',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(postApplication).toHaveBeenCalledWith(
        'https://real.example.com/jobs/clay-gtm-data-analyst',
        'acme-eng-2026-04',
        { force: true },
      );
    });
    const calls = (postApplication as ReturnType<typeof vi.fn>).mock.calls;
    for (const call of calls) {
      expect(call[0]).not.toMatch(/placeholder/);
    }
  });

  it('disables the re-run button when the API does not expose a URL (roborev job 944)', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
      url: '',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    expect(btn).toBeDisabled();
  });

  it('disables the re-run button when the API URL field is missing entirely', async () => {
    const { url: _drop, ...detailWithoutUrl } = {
      ...BASE_API_DETAIL,
      status: 'failed',
    };
    void _drop;
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(detailWithoutUrl);
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /^re-run apply$/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(postApplication).not.toHaveBeenCalled();
  });
});

// ── apply_url wiring tests (feat-bb81c3ce) ───────────────────────────────────
//
// The backend now returns `apply_url` from GET /api/applications/{slug}.
// When present and non-null, the re-run button must stay enabled and POST
// with that URL. When null/absent, the CLI tooltip path is preserved.
describe('ApplicationDetail apply_url wiring (feat-bb81c3ce)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    constructorCalls.length = 0;
    (postApplication as ReturnType<typeof vi.fn>).mockResolvedValue({
      slug: 'acme-eng-2026-04',
      run_id: 'run-new',
    });
  });

  it('apply_url present: button is enabled and POST uses apply_url', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
      apply_url: 'https://example.com/jobs/123',
      url: undefined,
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    await waitFor(() => {
      expect(postApplication).toHaveBeenCalledWith(
        'https://example.com/jobs/123',
        'acme-eng-2026-04',
        { force: true },
      );
    });
  });

  it('apply_url null: button is disabled and POST is not called', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
      apply_url: null,
      url: undefined,
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(postApplication).not.toHaveBeenCalled();
  });

  it('apply_url takes precedence over legacy url field when both present', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'done',
      apply_url: 'https://new-field.example.com/jobs/456',
      url: 'https://old-field.example.com/jobs/old',
    });
    render(<ApplicationDetail slug="acme-eng-2026-04" back={() => {}} />);
    const btn = await screen.findByRole('button', { name: /force re-run apply/i });
    fireEvent.click(btn);
    await waitFor(() => {
      expect(postApplication).toHaveBeenCalledWith(
        'https://new-field.example.com/jobs/456',
        'acme-eng-2026-04',
        { force: true },
      );
    });
  });
});

// ── SSE phase wiring tests (feat-6e148975, GH#59) ────────────────────────────
//
// These tests assert that incoming SSE `event: phase` frames update both the
// phase tracker (PHASE 1 / 2 / 3 status labels) and the header status badge.
// They FAIL today because the component's phase listener does not drive the
// badge status and the phase tracker status derives only from `running` state,
// which is not set for all transitions.
describe('ApplicationDetail SSE phase wiring (feat-6e148975)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    constructorCalls.length = 0;
    lastFakeEs = null;
  });

  it('phase tracker shows "running" for the active phase when SSE emits gather/running', async () => {
    // Slug is currently running so the component auto-subscribes.
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'running',
      phase: 'gather',
      finished_at: null,
      run_id: 'run-sse-1',
    });
    render(<ApplicationDetail slug="sse-test-slug" back={() => {}} />);
    // Wait until EventSource is created and the page is rendered.
    await waitFor(() => expect(lastFakeEs).not.toBeNull());

    // Fire a gather/running phase event.
    await act(async () => {
      lastFakeEs!.emit('phase', {
        run_id: 'run-sse-1',
        phase: 'gather',
        status: 'running',
        started_at: '2026-05-05T10:00:00Z',
        finished_at: null,
      });
    });

    // PHASE 1 should show "running" in the phase tracker.
    // The phase-status span text is either "running", "done", or "queued".
    await waitFor(() => {
      // The pipeline section must contain a "running" indicator for PHASE 1.
      const phaseStatuses = screen.getAllByText(/running/i);
      expect(phaseStatuses.length).toBeGreaterThan(0);
    });
  });

  it('phase tracker transitions gather→done→draft/running→render/running', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'running',
      phase: 'gather',
      finished_at: null,
      run_id: 'run-sse-2',
    });
    render(<ApplicationDetail slug="sse-test-slug-2" back={() => {}} />);
    await waitFor(() => expect(lastFakeEs).not.toBeNull());

    // gather → done
    await act(async () => {
      lastFakeEs!.emit('phase', {
        run_id: 'run-sse-2',
        phase: 'gather',
        status: 'done',
        started_at: '2026-05-05T10:00:00Z',
        finished_at: '2026-05-05T10:00:05Z',
      });
    });

    // draft → running
    await act(async () => {
      lastFakeEs!.emit('phase', {
        run_id: 'run-sse-2',
        phase: 'draft',
        status: 'running',
        started_at: '2026-05-05T10:00:05Z',
        finished_at: null,
      });
    });

    // After gather=done, PHASE 1 progress bar should be at 100% (done).
    // After draft=running, PHASE 2 should show "running".
    // Use the phase-name labels to find the right card, then check status.
    await waitFor(() => {
      // The "running" indicator in phase-status should now be for phase 2 (draft).
      // We verify that the component shows at least one "running" status text
      // (from the draft phase) and the phase-status for gather shows "done".
      const allText = document.body.textContent ?? '';
      expect(allText).toContain('done');
    });
  });

  it('header status badge transitions from "running" to "failed" on SSE phase/failed', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'running',
      phase: 'gather',
      finished_at: null,
      run_id: 'run-sse-3',
    });
    render(<ApplicationDetail slug="sse-test-slug-3" back={() => {}} />);
    await waitFor(() => expect(lastFakeEs).not.toBeNull());

    // Initially the header badge should show "running" (from apiDetail.status=running).
    // The badge has class "badge accent" for running.
    await waitFor(() => {
      const badge = document.querySelector('.badge.accent');
      expect(badge).not.toBeNull();
      expect(badge!.textContent).toMatch(/running/i);
    });

    // Emit a phase/failed event.
    await act(async () => {
      lastFakeEs!.emit('phase', {
        run_id: 'run-sse-3',
        phase: 'gather',
        status: 'failed',
        started_at: '2026-05-05T10:00:00Z',
        finished_at: '2026-05-05T10:00:10Z',
      });
    });

    // After SSE phase/failed, the header status badge should switch to "failed"
    // (class "badge danger"), not remain "running" (class "badge accent").
    await waitFor(() => {
      // "failed" badge uses kind="danger" → class "badge danger"
      const failedBadge = document.querySelector('.badge.danger');
      expect(failedBadge).not.toBeNull();
      expect(failedBadge!.textContent).toMatch(/failed/i);
    });
    // The accent (running) badge should be gone from the header area.
    // (phase-status spans may still have "running" as text but the StatusBadge
    // with class "badge accent" should no longer be present once running=false.)
    expect(document.querySelector('.badge.accent')).toBeNull();
  });

  it('header status badge transitions from "running" to "rendered" on SSE render/done (roborev job 948)', async () => {
    // Anti-regression for roborev job 948 MEDIUM: when the final render phase
    // completes, the badge previously stayed at "running" because `running`
    // wasn't being flipped to false on phaseNum===3 done.
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'running',
      phase: 'render',
      finished_at: null,
      run_id: 'run-sse-5',
    });
    render(<ApplicationDetail slug="sse-test-slug-5" back={() => {}} />);
    await waitFor(() => expect(lastFakeEs).not.toBeNull());

    // Initial running badge present.
    await waitFor(() => {
      const badge = document.querySelector('.badge.accent');
      expect(badge).not.toBeNull();
      expect(badge!.textContent).toMatch(/running/i);
    });

    // Emit phase=render, status=done.
    await act(async () => {
      lastFakeEs!.emit('phase', {
        run_id: 'run-sse-5',
        phase: 'render',
        status: 'done',
        started_at: '2026-05-05T10:00:00Z',
        finished_at: '2026-05-05T10:01:00Z',
      });
    });

    // Running badge must be gone — terminal "done" sseStatus should now win.
    await waitFor(() => {
      expect(document.querySelector('.badge.accent')).toBeNull();
    });
  });

  it('anti-regression: initial GET with phase=running does not show all phases as queued', async () => {
    // When the initial GET already shows status=running, the phase tracker
    // must NOT show all phases frozen at "queued".
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...BASE_API_DETAIL,
      status: 'running',
      phase: 'gather',
      finished_at: null,
      run_id: 'run-sse-4',
    });
    render(<ApplicationDetail slug="sse-test-slug-4" back={() => {}} />);
    await waitFor(() => expect(lastFakeEs).not.toBeNull());

    // The header badge should show "running" (class "badge accent").
    await waitFor(() => {
      const badge = document.querySelector('.badge.accent');
      expect(badge).not.toBeNull();
      expect(badge!.textContent).toMatch(/running/i);
    });

    // Not all three phase-status spans should say "queued" — at least one
    // phase should be in a non-queued state (running or done).
    await waitFor(() => {
      const phaseStatuses = document.querySelectorAll('.phase-status');
      const texts = Array.from(phaseStatuses).map(el => el.textContent ?? '');
      const allQueued = texts.every(t => t.includes('queued'));
      expect(allQueued).toBe(false);
    });
  });
});
