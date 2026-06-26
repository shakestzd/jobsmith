// OfflineModeToggle.test.tsx — unit tests for the desktop offline-mode panel.
//
// Assertions:
//   renders nothing while checking and on a non-desktop server (status 404)
//   reachable backend → shows the detected server base_url (+ model)
//   not reachable → shows the runtime/install hint
//   clicking Enable surfaces the deferred "pending plan-938f735b" notice
//   (never crashes, never silently no-ops — REDUCED SCOPE, slice 7)

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    getLlmStatus: vi.fn(),
    enableOfflineMode: vi.fn(),
  };
});

import { getLlmStatus, enableOfflineMode, JobsmithApiError } from '../api/client';
import { OfflineModeToggle } from './OfflineModeToggle';

const mockStatus = getLlmStatus as ReturnType<typeof vi.fn>;
const mockEnable = enableOfflineMode as ReturnType<typeof vi.fn>;

const offline = {
  mlx: { reachable: false, base_url: 'http://127.0.0.1:8080', runtime_installed: false, model: null },
  ollama: { reachable: false, base_url: 'http://127.0.0.1:11434', runtime_installed: false, model: null },
};

describe('OfflineModeToggle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders nothing on a non-desktop server (404)', async () => {
    mockStatus.mockRejectedValue(new JobsmithApiError('Not Found', 404));
    const { container } = render(<OfflineModeToggle />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it('shows a detected MLX server with its base_url and model', async () => {
    mockStatus.mockResolvedValue({
      ...offline,
      mlx: { reachable: true, base_url: 'http://127.0.0.1:8080', runtime_installed: true, model: 'qwen2.5-coder' },
    });
    render(<OfflineModeToggle />);
    const row = await screen.findByTestId('offline-backend-mlx');
    expect(row).toHaveTextContent('http://127.0.0.1:8080');
    expect(row).toHaveTextContent('qwen2.5-coder');
  });

  it('shows the install hint when a backend is not detected', async () => {
    mockStatus.mockResolvedValue(offline);
    render(<OfflineModeToggle />);
    const row = await screen.findByTestId('offline-backend-ollama');
    expect(row).toHaveTextContent('Not detected');
    expect(screen.getByRole('link', { name: 'Download Ollama' })).toBeInTheDocument();
  });

  it('shows the "runtime installed but not running" hint', async () => {
    mockStatus.mockResolvedValue({
      ...offline,
      mlx: { reachable: false, base_url: 'http://127.0.0.1:8080', runtime_installed: true, model: null },
    });
    render(<OfflineModeToggle />);
    const row = await screen.findByTestId('offline-backend-mlx');
    expect(row).toHaveTextContent('Runtime installed but not running');
    expect(row).toHaveTextContent('mlx_lm.server');
  });

  it('Enable surfaces the deferred pending-plan-938f735b notice (no crash)', async () => {
    mockStatus.mockResolvedValue(offline);
    mockEnable.mockResolvedValue({
      status: 'unavailable',
      reason: 'offline backend config pending plan-938f735b',
    });
    render(<OfflineModeToggle />);
    const btn = await screen.findByRole('button', { name: 'Enable offline mode' });
    fireEvent.click(btn);
    await waitFor(() => expect(mockEnable).toHaveBeenCalledTimes(1));
    const notice = await screen.findByTestId('offline-pending');
    expect(notice).toHaveTextContent('plan-938f735b');
  });

  it('Enable degrades gracefully even when the request errors', async () => {
    mockStatus.mockResolvedValue(offline);
    mockEnable.mockRejectedValue(new JobsmithApiError('Internal Server Error', 500));
    render(<OfflineModeToggle />);
    fireEvent.click(await screen.findByRole('button', { name: 'Enable offline mode' }));
    await waitFor(() => expect(mockEnable).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId('offline-pending')).toHaveTextContent('Internal Server Error');
  });
});
