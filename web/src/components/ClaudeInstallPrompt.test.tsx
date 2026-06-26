// ClaudeInstallPrompt.test.tsx — unit tests for the desktop `claude` CLI prompt.
//
// Assertions:
//   renders nothing while checking and when claude is already installed
//   renders nothing on a non-desktop server (status 404)
//   renders the guided native-installer command when claude is missing
//   clicking Re-check re-probes; once installed it confirms and hides the prompt

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    getDepsStatus: vi.fn(),
  };
});

import { getDepsStatus, JobsmithApiError } from '../api/client';
import { ClaudeInstallPrompt } from './ClaudeInstallPrompt';

const mockStatus = getDepsStatus as ReturnType<typeof vi.fn>;

describe('ClaudeInstallPrompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing when claude is already installed', async () => {
    mockStatus.mockResolvedValue({ claude_installed: true, version: '2.1.15', path: '/usr/local/bin/claude' });
    const { container } = render(<ClaudeInstallPrompt />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing on a non-desktop server (404)', async () => {
    mockStatus.mockRejectedValue(new JobsmithApiError('Not Found', 404));
    const { container } = render(<ClaudeInstallPrompt />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it('shows the native-installer command when claude is missing', async () => {
    mockStatus.mockResolvedValue({ claude_installed: false, version: null, path: null });
    render(<ClaudeInstallPrompt />);
    expect(await screen.findByText('Claude Code CLI required')).toBeInTheDocument();
    expect(screen.getByTestId('claude-install-command')).toHaveTextContent(
      'curl -fsSL https://claude.ai/install.sh | bash',
    );
    expect(screen.getByRole('button', { name: 'Re-check' })).toBeInTheDocument();
  });

  it('re-check after install confirms and hides the prompt', async () => {
    // First probe: missing. Re-check probe: installed.
    mockStatus
      .mockResolvedValueOnce({ claude_installed: false, version: null, path: null })
      .mockResolvedValueOnce({ claude_installed: true, version: '2.1.15', path: '/usr/local/bin/claude' });

    render(<ClaudeInstallPrompt />);
    const btn = await screen.findByRole('button', { name: 'Re-check' });
    fireEvent.click(btn);

    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(2));
    expect(
      await screen.findByText('Claude Code CLI detected — the apply pipeline is ready.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('Claude Code CLI required')).not.toBeInTheDocument();
  });

  it('keeps the prompt visible with a message when a re-check errors', async () => {
    mockStatus
      .mockResolvedValueOnce({ claude_installed: false, version: null, path: null })
      .mockRejectedValueOnce(new JobsmithApiError('Internal Server Error', 500));

    render(<ClaudeInstallPrompt />);
    fireEvent.click(await screen.findByRole('button', { name: 'Re-check' }));

    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Internal Server Error')).toBeInTheDocument();
    expect(screen.getByText('Claude Code CLI required')).toBeInTheDocument();
  });
});
