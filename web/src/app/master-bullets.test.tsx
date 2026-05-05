// master-bullets.test.tsx — tests for BulletEditor bullet operations.
// feat-c41539d5: mark anchor / drop anchor / add bullet / remove bullet.
//
// Covers:
// (a) Correct pills rendered for anchored vs non-anchored bullets.
// (b) "mark anchor" pill → POST anchor endpoint; refetch triggered; buttons
//     disabled while in flight.
// (c) "drop" pill → custom modal; empty submit rejected; valid drop_reason →
//     POST anchor with body.
// (d) "+ add bullet" button → inline form; submit → POST bullets endpoint.
// (e) "remove" pill → custom modal; empty reason rejected; valid reason →
//     DELETE with body.
// Anti-regression: window.confirm / window.prompt are never called.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react';
import { MasterContent } from './master';

// ── Shared mock refetch tracker ───────────────────────────────────────────

const mockRefetch = vi.fn();

// ── Mock hooks ────────────────────────────────────────────────────────────

vi.mock('../api/hooks', () => ({
  useMasterSection: vi.fn(() => ({ data: undefined, isLoading: false, error: null })),
  useMasterSectionWithMeta: vi.fn(() => ({
    data: undefined,
    etag: null,
    isLoading: false,
    error: null,
    refetch: mockRefetch,
  })),
  JobsmithApiError: class JobsmithApiError extends Error {
    status = 500;
  },
}));

// ── Mock api/client ───────────────────────────────────────────────────────

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
  apiGetWithMeta: vi.fn(),
  formatDetail: vi.fn((_, fb) => fb),
  JobsmithApiError: class JobsmithApiError extends Error {
    status: number;
    constructor(msg: string, status = 500) {
      super(msg);
      this.status = status;
    }
  },
}));

import { apiPost, apiDelete } from '../api/client';
import { useMasterSectionWithMeta } from '../api/hooks';

// ── Fixture ───────────────────────────────────────────────────────────────

const WORK_FIXTURE = [
  {
    title: 'Senior Engineer',
    location: 'Acme Corp',
    date: '2020–2024',
    details: [
      { bullet: 'Deployed the pipeline', anchor: true },
      'Wrote the docs',
    ],
  },
];

function setupWorkHook(refetch = mockRefetch) {
  (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockReturnValue({
    data: WORK_FIXTURE,
    etag: null,
    isLoading: false,
    error: null,
    refetch,
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────

function renderMasterWork() {
  render(<MasterContent />);
}

// ── Tests ─────────────────────────────────────────────────────────────────

describe('BulletEditor bullet operations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ role_index: 0, bullet_index: 0, action: 'anchor' });
    (apiDelete as ReturnType<typeof vi.fn>).mockResolvedValue({ role_index: 0, bullet_index: 1, action: 'remove' });
  });

  // (a) Correct pills for anchored vs non-anchored bullets
  it('(a) renders "⚑ anchor" + "drop" pill for anchored bullet and "mark anchor" for non-anchored', () => {
    setupWorkHook();
    renderMasterWork();

    // Anchored bullet: "Deployed the pipeline"
    expect(screen.getByText('⚑ anchor')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'drop' })).toBeInTheDocument();

    // Non-anchored bullet: "Wrote the docs"
    expect(screen.getByRole('button', { name: 'mark anchor' })).toBeInTheDocument();
  });

  // (b) "mark anchor" click → POST anchor; refetch triggered; buttons disabled in-flight
  it('(b) "mark anchor" fires POST anchor, refetch on success, buttons disabled in-flight', async () => {
    let resolvePost!: (v: unknown) => void;
    (apiPost as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise((r) => { resolvePost = r; }),
    );
    setupWorkHook();
    renderMasterWork();

    const markBtn = screen.getByRole('button', { name: 'mark anchor' });
    fireEvent.click(markBtn);

    // While in-flight, buttons are disabled.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'mark anchor' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'drop' })).toBeDisabled();
    });

    expect(apiPost).toHaveBeenCalledWith(
      '/api/master/work/roles/0/bullets/1/anchor',
      {},
    );

    await act(async () => { resolvePost({ role_index: 0, bullet_index: 1, action: 'anchor' }); });

    await waitFor(() => expect(mockRefetch).toHaveBeenCalled());
  });

  // (c) "drop" pill → custom modal; empty submit rejected; valid → POST with body
  it('(c) "drop" opens custom modal; empty drop_reason rejected; valid fires POST with drop_reason', async () => {
    setupWorkHook();
    renderMasterWork();

    fireEvent.click(screen.getByRole('button', { name: 'drop' }));

    // Custom modal appears — no window.confirm/prompt.
    const dialog = screen.getByRole('dialog', { name: /drop anchor/i });
    expect(dialog).toBeInTheDocument();

    // Empty submit is rejected.
    const dropAnchorBtn = screen.getByRole('button', { name: /drop anchor/i });
    fireEvent.click(dropAnchorBtn);
    expect(screen.getByText('drop reason is required')).toBeInTheDocument();
    expect(apiPost).not.toHaveBeenCalled();

    // Valid reason → POST with drop_reason.
    const textarea = screen.getByPlaceholderText('drop reason…');
    fireEvent.change(textarea, { target: { value: 'no longer relevant' } });
    fireEvent.click(dropAnchorBtn);

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        '/api/master/work/roles/0/bullets/0/anchor',
        { drop_reason: 'no longer relevant' },
      );
    });
    await waitFor(() => expect(mockRefetch).toHaveBeenCalled());

    // Modal closes after success.
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /drop anchor/i })).toBeNull());
  });

  // (d) "+ add bullet" inline form → POST bullets with { text, position }
  it('(d) "+ add bullet" shows inline form; submit fires POST bullets with text and position', async () => {
    setupWorkHook();
    renderMasterWork();

    fireEvent.click(screen.getByRole('button', { name: '+ add bullet' }));

    const textarea = screen.getByPlaceholderText('bullet text…');
    const posInput = screen.getByRole('spinbutton'); // number input

    fireEvent.change(textarea, { target: { value: 'Launched new feature' } });
    fireEvent.change(posInput, { target: { value: '2' } });

    fireEvent.click(screen.getByRole('button', { name: 'add' }));

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        '/api/master/work/roles/0/bullets',
        { text: 'Launched new feature', position: 2 },
      );
    });
    await waitFor(() => expect(mockRefetch).toHaveBeenCalled());

    // Inline form dismisses after success.
    await waitFor(() => expect(screen.queryByPlaceholderText('bullet text…')).toBeNull());
  });

  // (d) add without position → omits position key
  it('(d) add bullet without position omits position from POST body', async () => {
    setupWorkHook();
    renderMasterWork();

    fireEvent.click(screen.getByRole('button', { name: '+ add bullet' }));
    const textarea = screen.getByPlaceholderText('bullet text…');
    fireEvent.change(textarea, { target: { value: 'Append at end' } });
    // Do not fill position.
    fireEvent.click(screen.getByRole('button', { name: 'add' }));

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        '/api/master/work/roles/0/bullets',
        { text: 'Append at end' },
      );
    });
  });

  // (e) "remove" pill → custom modal; empty reason rejected; valid → DELETE
  it('(e) "remove" opens custom modal; empty reason rejected; valid fires DELETE with reason', async () => {
    setupWorkHook();
    renderMasterWork();

    // Both bullets have a "remove" button.
    const removeButtons = screen.getAllByRole('button', { name: 'remove' });
    fireEvent.click(removeButtons[1]); // second bullet (non-anchored, index 1)

    const dialog = screen.getByRole('dialog', { name: /remove bullet/i });
    expect(dialog).toBeInTheDocument();

    // Empty submit rejected — use `within` to scope to the modal dialog.
    const removeBtn = within(dialog).getByRole('button', { name: /^remove$/ });
    fireEvent.click(removeBtn);
    expect(screen.getByText('reason is required')).toBeInTheDocument();
    expect(apiDelete).not.toHaveBeenCalled();

    // Valid reason → DELETE.
    const textarea = screen.getByPlaceholderText('reason…');
    fireEvent.change(textarea, { target: { value: 'outdated information' } });
    fireEvent.click(removeBtn);

    await waitFor(() => {
      expect(apiDelete).toHaveBeenCalledWith(
        '/api/master/work/roles/0/bullets/1',
        { reason: 'outdated information' },
      );
    });
    await waitFor(() => expect(mockRefetch).toHaveBeenCalled());

    // Modal closes.
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /remove bullet/i })).toBeNull());
  });

  // Anti-regression: no window.confirm / window.prompt
  it('never calls window.confirm or window.prompt for any bullet operation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('reason');

    setupWorkHook();
    renderMasterWork();

    // Click mark anchor.
    fireEvent.click(screen.getByRole('button', { name: 'mark anchor' }));
    await waitFor(() => expect(apiPost).toHaveBeenCalled());

    // Open drop modal.
    fireEvent.click(screen.getByRole('button', { name: 'drop' }));
    // Open remove modal for first bullet.
    const removeButtons = screen.getAllByRole('button', { name: 'remove' });
    fireEvent.click(removeButtons[0]);

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(promptSpy).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
    promptSpy.mockRestore();
  });
});
