// new.test.tsx — accessibility + button type tests for NewApplicationModal
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { NewApplicationModal } from './new';

// ── API mock (MSW-style vi.mock pattern, consistent with dashboard/feedback tests) ──

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  postApplication: vi.fn(),
  buildEventsUrl: vi.fn(),
  // BrowserInstallPrompt + ClaudeInstallPrompt are now mounted in step 1.
  // Resolve as "already installed" so they immediately hide (render null)
  // without blocking any existing test assertions.
  getBrowserStatus: vi.fn().mockResolvedValue({ installed: true }),
  getDepsStatus: vi.fn().mockResolvedValue({ claude_installed: true }),
  installBrowser: vi.fn(),
  buildBrowserInstallEventsUrl: vi.fn().mockReturnValue(''),
  redactSensitive: vi.fn((s: string) => s),
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

import { apiGet } from '../api/client';

const DOCTOR_FIXTURE = [
  {
    name: 'claude_binary',
    status: 'pass' as const,
    message: 'FROM_API_FIXTURE_claude_v9.99.99',
  },
  {
    name: 'claude_auth',
    status: 'pass' as const,
    message: 'authenticated as test@example.com (pro)',
  },
  {
    name: 'python_version',
    status: 'pass' as const,
    message: 'Python 3.12.0 >= 3.10',
  },
];

describe('NewApplicationModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: doctor returns fixture data
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue(DOCTOR_FIXTURE);
  });

  it('renders with role=dialog and aria-modal', () => {
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={vi.fn()} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });

  it('pressing Escape calls onClose', () => {
    const onClose = vi.fn();
    render(<NewApplicationModal onClose={onClose} onLaunch={vi.fn()} />);
    const dialog = screen.getByRole('dialog');
    fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('cancel button has type="button" and clicking it does not submit the form', () => {
    const onClose = vi.fn();
    const onLaunch = vi.fn();
    render(<NewApplicationModal onClose={onClose} onLaunch={onLaunch} />);
    const cancelBtn = screen.getByRole('button', { name: /cancel/i });
    expect(cancelBtn).toHaveAttribute('type', 'button');
    fireEvent.click(cancelBtn);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onLaunch).not.toHaveBeenCalled();
  });

  it('review button has type="button" and advances to step 2', () => {
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={vi.fn()} />);
    const reviewBtn = screen.getByRole('button', { name: /review/i });
    expect(reviewBtn).toHaveAttribute('type', 'button');
    fireEvent.click(reviewBtn);
    expect(screen.getByText(/step 2 of 2/i)).toBeInTheDocument();
  });

  it('apply button on step 2 calls onLaunch with slug and url', () => {
    const onLaunch = vi.fn();
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={onLaunch} />);
    // advance to step 2
    fireEvent.click(screen.getByRole('button', { name: /review/i }));
    const applyBtn = screen.getByRole('button', { name: /apply/i });
    expect(applyBtn).toHaveAttribute('type', 'submit');
    fireEvent.click(applyBtn);
    expect(onLaunch).toHaveBeenCalledTimes(1);
    // first arg: locally-derived slug; second arg: the raw job URL;
    // third arg (jdText): undefined when "fetch from url" is the active mode
    expect(onLaunch.mock.calls[0][0]).toMatch(/linear-product-engineer/);
    expect(onLaunch.mock.calls[0][1]).toMatch(/^https?:\/\//);
    expect(onLaunch.mock.calls[0][2]).toBeUndefined();
  });

  // bug-1c800e09: paste-text mode must propagate jdText to onLaunch so the
  // backend can pass --jd-text-file instead of trying to fetch the URL.
  it('paste-text mode forwards pasted JD text as jdText to onLaunch', () => {
    const onLaunch = vi.fn();
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={onLaunch} />);

    // activate paste mode
    fireEvent.click(screen.getByText(/paste text/i));

    // type into the now-visible textarea
    const ta = screen.getByPlaceholderText(/paste the full job description/i);
    fireEvent.change(ta, { target: { value: 'Senior Data Engineer at TestCo' } });

    fireEvent.click(screen.getByRole('button', { name: /review/i }));
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(onLaunch).toHaveBeenCalledTimes(1);
    expect(onLaunch.mock.calls[0][2]).toBe('Senior Data Engineer at TestCo');
  });

  it('switching back from paste to fetch mode clears jdText from onLaunch payload', () => {
    const onLaunch = vi.fn();
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={onLaunch} />);

    fireEvent.click(screen.getByText(/paste text/i));
    const ta = screen.getByPlaceholderText(/paste the full job description/i);
    fireEvent.change(ta, { target: { value: 'should not survive mode switch' } });
    fireEvent.click(screen.getByText(/fetch from url/i));

    fireEvent.click(screen.getByRole('button', { name: /review/i }));
    fireEvent.click(screen.getByRole('button', { name: /apply/i }));

    expect(onLaunch.mock.calls[0][2]).toBeUndefined();
  });

  it('pressing Enter in the URL field does not trigger cancel', () => {
    const onClose = vi.fn();
    render(<NewApplicationModal onClose={onClose} onLaunch={vi.fn()} />);
    const urlInput = screen.getByPlaceholderText('https://...');
    fireEvent.keyDown(urlInput, { key: 'Enter', code: 'Enter' });
    expect(onClose).not.toHaveBeenCalled();
  });

  // ── DOD: live API data reaches the preflight panel ────────────────────────

  it('step 2 preflight panel renders sentinel value from /api/doctor response', async () => {
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /review/i }));

    await waitFor(() => {
      expect(screen.getByText('FROM_API_FIXTURE_claude_v9.99.99')).toBeInTheDocument();
    });
  });

  it('step 2 preflight panel shows "checking…" while /api/doctor is loading', () => {
    (apiGet as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {})); // never resolves
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /review/i }));
    expect(screen.getByText('checking…')).toBeInTheDocument();
  });

  it('step 2 preflight panel shows error message when /api/doctor fails', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('401 Unauthorized'));
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /review/i }));

    await waitFor(() => {
      expect(screen.getByText(/preflight unavailable/i)).toBeInTheDocument();
    });
  });

  // ── DOD: anti-regression — no hardcoded fixture values survive in DOM ────

  it('hardcoded fixture strings are absent from rendered modal when API returns any response', async () => {
    // Single render: walk to step 2, wait for the API sentinel to confirm
    // the live data path actually reached the DOM, then assert the legacy
    // fixture strings are absent on that same container. This proves the
    // negative against an actually-rendered preflight panel — not against
    // an empty/loading state where everything would trivially be missing.
    // (roborev job 943 fix.)
    const { container } = render(
      <NewApplicationModal onClose={vi.fn()} onLaunch={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /review/i }));

    await waitFor(() => {
      expect(screen.getByText('FROM_API_FIXTURE_claude_v9.99.99')).toBeInTheDocument();
    });

    const text = container.textContent ?? '';
    expect(text).not.toContain('v1.4.0');
    expect(text).not.toContain('v1.5.57');
    expect(text).not.toContain('38 bullets');
    expect(text).not.toContain('7 prior runs');
    expect(text).not.toContain('claude CLI');
  });
});
