// BrowserInstallPrompt.test.tsx — unit tests for the desktop Chromium prompt.
//
// Assertions:
//   renders nothing while checking and when Chromium is already installed
//   renders nothing on a non-desktop server (status 404)
//   renders the prompt when Chromium is missing
//   clicking Download → POST install + opens SSE + streams progress → done
//   error events surface a retryable message + Retry button

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    getBrowserStatus: vi.fn(),
    installBrowser: vi.fn(),
    buildBrowserInstallEventsUrl: vi.fn(() => 'http://localhost/api/desktop/browser/install/events?token=t'),
  };
});

import {
  getBrowserStatus,
  installBrowser,
  JobsmithApiError,
} from '../api/client';
import { BrowserInstallPrompt } from './BrowserInstallPrompt';

// ── Fake EventSource ───────────────────────────────────────────────────────
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  listeners: Record<string, (e: MessageEvent) => void> = {};
  onerror: (() => void) | null = null;
  closed = false;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, cb: (e: MessageEvent) => void) {
    this.listeners[type] = cb;
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data: unknown) {
    this.listeners[type]?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

const mockStatus = getBrowserStatus as ReturnType<typeof vi.fn>;
const mockInstall = installBrowser as ReturnType<typeof vi.fn>;

describe('BrowserInstallPrompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    FakeEventSource.instances = [];
    // @ts-expect-error — inject a jsdom-friendly EventSource stub
    globalThis.EventSource = FakeEventSource;
  });

  afterEach(() => {
    // @ts-expect-error — cleanup
    delete globalThis.EventSource;
  });

  it('renders nothing when Chromium is already installed', async () => {
    mockStatus.mockResolvedValue({ installed: true, path: '/x' });
    const { container } = render(<BrowserInstallPrompt />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing on a non-desktop server (404)', async () => {
    mockStatus.mockRejectedValue(new JobsmithApiError('Not Found', 404));
    const { container } = render(<BrowserInstallPrompt />);
    await waitFor(() => expect(mockStatus).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it('shows the prompt when Chromium is missing', async () => {
    mockStatus.mockResolvedValue({ installed: false, path: '/x' });
    render(<BrowserInstallPrompt />);
    expect(await screen.findByText('Browser download required')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download browser' })).toBeInTheDocument();
  });

  it('downloads, streams progress, and reaches done', async () => {
    mockStatus.mockResolvedValue({ installed: false, path: '/x' });
    mockInstall.mockResolvedValue({ status: 'started' });
    render(<BrowserInstallPrompt />);

    const btn = await screen.findByRole('button', { name: 'Download browser' });
    fireEvent.click(btn);

    await waitFor(() => expect(mockInstall).toHaveBeenCalled());
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));
    const es = FakeEventSource.instances[0];

    es.emit('progress', { phase: 'progress', message: 'Downloading Chromium 50%' });
    expect(await screen.findByText(/Downloading Chromium 50%/)).toBeInTheDocument();

    es.emit('progress', { phase: 'done', message: 'Chromium installed.' });
    expect(
      await screen.findByText('Browser installed — JS-rendered pages are now supported.'),
    ).toBeInTheDocument();
    expect(es.closed).toBe(true);
  });

  it('surfaces an error event with a Retry button', async () => {
    mockStatus.mockResolvedValue({ installed: false, path: '/x' });
    mockInstall.mockResolvedValue({ status: 'started' });
    render(<BrowserInstallPrompt />);

    fireEvent.click(await screen.findByRole('button', { name: 'Download browser' }));
    await waitFor(() => expect(FakeEventSource.instances.length).toBe(1));

    FakeEventSource.instances[0].emit('progress', {
      phase: 'error',
      message: 'playwright install exited with 1',
    });

    expect(await screen.findByText('Download failed.')).toBeInTheDocument();
    expect(screen.getByText('playwright install exited with 1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});
