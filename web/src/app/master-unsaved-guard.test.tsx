// master-unsaved-guard.test.tsx — unsaved-changes guard for tab-switch (feat-815279db).
//
// Covers:
//   (a) Edit Skill → click Education tab → AlertDialog appears with three buttons
//   (b) Cancel → still on Skill tab; edited value is intact
//   (c) "Discard & switch" → on Education tab; switching back shows original value
//   (d) "Save & switch" → PUT fires; on success switches to Education tab
//   (e) Edit Skill → fire window 'beforeunload' → returnValue set to non-empty string

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MasterContent } from './master';

// vi.mock is hoisted — factory must not reference local variables.
const { MockApiError } = vi.hoisted(() => {
  class MockApiError extends Error {
    status: number;
    constructor(msg: string, status: number) {
      super(msg);
      this.status = status;
      this.name = 'JobsmithApiError';
    }
  }
  return { MockApiError };
});

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
  apiGetWithMeta: vi.fn(),
  formatDetail: vi.fn((_raw: unknown, fallback: string) => fallback),
  JobsmithApiError: MockApiError,
}));

vi.mock('../api/hooks', () => ({
  useMasterSection: vi.fn(() => ({ data: undefined, isLoading: false, error: null })),
  useMasterSectionWithMeta: vi.fn(() => ({
    data: undefined,
    etag: null,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  })),
}));

import { apiPut } from '../api/client';
import { useMasterSectionWithMeta } from '../api/hooks';

// Skill fixture with one editable group
const SKILL_DATA = [
  { title: 'Languages', description: 'Python, TypeScript', details: ['typed'] },
];

// Education fixture
const EDUCATION_DATA = [
  { title: 'B.S. Computer Science', organization: 'State University', date: '2015', details: [] },
];

/**
 * Helper: configure the useMasterSectionWithMeta mock to return section-specific
 * data. The hook is called with different section names by different tabs.
 */
function setupSectionMock(overrides: Record<string, { data: unknown; etag: string; refetch: ReturnType<typeof vi.fn> }>) {
  (useMasterSectionWithMeta as ReturnType<typeof vi.fn>).mockImplementation((section: string) => {
    const override = overrides[section];
    if (override) {
      return { data: override.data, etag: override.etag, isLoading: false, error: null, refetch: override.refetch };
    }
    return { data: undefined, etag: null, isLoading: false, error: null, refetch: vi.fn() };
  });
}

describe('Unsaved-changes guard — tab switch (feat-815279db)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    // Clean up any lingering listeners
    vi.restoreAllMocks();
  });

  // ── (a) AlertDialog appears when switching away from dirty tab ───────────

  it('(a) editing Skill then clicking Education tab shows guard dialog with three buttons', async () => {
    const refetchSkill = vi.fn();
    setupSectionMock({
      skill: { data: SKILL_DATA, etag: '"etag-skill"', refetch: refetchSkill },
    });

    render(<MasterContent />);

    // Navigate to skill tab
    fireEvent.click(screen.getByText(/skill\.yml/i));

    // Wait for data to render
    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript')).toBeInTheDocument();
    });

    // Edit a field to make the tab dirty
    fireEvent.change(screen.getByDisplayValue('Python, TypeScript'), {
      target: { value: 'Python, TypeScript, Rust' },
    });

    // Click Education tab — guard should intercept
    fireEvent.click(screen.getByText(/education\.yml/i));

    // AlertDialog should appear with all three action buttons
    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    expect(screen.getByRole('button', { name: /save & switch/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /discard & switch/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument();
  });

  // ── (b) Cancel stays on current tab with edits intact ───────────────────

  it('(b) clicking Cancel stays on Skill tab and preserves the edited value', async () => {
    const refetchSkill = vi.fn();
    setupSectionMock({
      skill: { data: SKILL_DATA, etag: '"etag-skill"', refetch: refetchSkill },
    });

    render(<MasterContent />);

    // Go to Skill tab
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript')).toBeInTheDocument();
    });

    // Make dirty
    fireEvent.change(screen.getByDisplayValue('Python, TypeScript'), {
      target: { value: 'Edited value' },
    });

    // Attempt to navigate away
    fireEvent.click(screen.getByText(/education\.yml/i));

    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    // Click Cancel
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));

    // Dialog should be dismissed
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    // Still on Skill tab — edited value is preserved
    expect(screen.getByDisplayValue('Edited value')).toBeInTheDocument();

    // Education tab content should NOT be visible
    expect(screen.queryByText(/B\.S\. Computer Science/i)).toBeNull();
  });

  // ── (c) Discard & switch — switches to target tab, original value after back-nav ──

  it('(c) "Discard & switch" navigates to Education; returning to Skill shows original value', async () => {
    const refetchSkill = vi.fn();
    const refetchEdu = vi.fn();
    setupSectionMock({
      skill: { data: SKILL_DATA, etag: '"etag-skill"', refetch: refetchSkill },
      education: { data: EDUCATION_DATA, etag: '"etag-edu"', refetch: refetchEdu },
    });

    render(<MasterContent />);

    // Go to Skill tab
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript')).toBeInTheDocument();
    });

    // Make dirty
    fireEvent.change(screen.getByDisplayValue('Python, TypeScript'), {
      target: { value: 'Dirty value, discard me' },
    });

    // Attempt nav to Education
    fireEvent.click(screen.getByText(/education\.yml/i));

    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    // Click Discard & switch
    fireEvent.click(screen.getByRole('button', { name: /discard & switch/i }));

    // Now on Education tab
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    // Navigate back to Skill — state should be reset to original
    fireEvent.click(screen.getByText(/skill\.yml/i));

    // No guard dialog this time (tab was not dirty)
    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript')).toBeInTheDocument();
    });

    // The dirty value is gone
    expect(screen.queryByDisplayValue('Dirty value, discard me')).toBeNull();
  });

  // ── (d) Save & switch — PUT fires, then navigates ───────────────────────

  it('(d) "Save & switch" fires PUT for Skill then navigates to Education tab on success', async () => {
    const refetchSkill = vi.fn();
    const refetchEdu = vi.fn();
    setupSectionMock({
      skill: { data: SKILL_DATA, etag: '"etag-skill"', refetch: refetchSkill },
      education: { data: EDUCATION_DATA, etag: '"etag-edu"', refetch: refetchEdu },
    });

    (apiPut as ReturnType<typeof vi.fn>).mockResolvedValue({
      section: 'skill', path: 'db:skill', bytes_written: 10,
    });

    render(<MasterContent />);

    // Go to Skill tab
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript')).toBeInTheDocument();
    });

    // Make dirty
    fireEvent.change(screen.getByDisplayValue('Python, TypeScript'), {
      target: { value: 'Python, TypeScript, Go' },
    });

    // Attempt nav to Education
    fireEvent.click(screen.getByText(/education\.yml/i));

    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    // Click Save & switch
    fireEvent.click(screen.getByRole('button', { name: /save & switch/i }));

    // PUT should have been called for /api/master/skill
    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith(
        '/api/master/skill',
        expect.anything(),
        expect.objectContaining({ ifMatch: '"etag-skill"' }),
      );
    });

    // After successful save, should navigate to Education tab
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    // Education tab content should now be active (no guard dialog interfered)
    // We verify that the Skill tab inputs are no longer shown
    await waitFor(() => {
      expect(screen.queryByDisplayValue('Python, TypeScript, Go')).toBeNull();
    });
  });

  // ── (d2) Save & switch on FAILED save stays on dirty tab (roborev job 950) ─

  it('(d2) "Save & switch" with a 412 conflict stays on Skill tab; edits preserved', async () => {
    // Regression: prior to roborev job 950 fix, doSave swallowed errors and
    // requestSave always returned true — so the guard would unmount the
    // dirty editor on a failed PUT, discarding the user's edits. Now the
    // guard MUST keep the user on the dirty tab when the PUT fails.
    const refetchSkill = vi.fn();
    setupSectionMock({
      skill: { data: SKILL_DATA, etag: '"etag-skill"', refetch: refetchSkill },
      education: { data: EDUCATION_DATA, etag: '"etag-edu"', refetch: vi.fn() },
    });

    const conflictErr = new MockApiError('Precondition Failed', 412);
    (apiPut as ReturnType<typeof vi.fn>).mockRejectedValue(conflictErr);

    render(<MasterContent />);
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript')).toBeInTheDocument();
    });

    // Make dirty.
    fireEvent.change(screen.getByDisplayValue('Python, TypeScript'), {
      target: { value: 'Python, TypeScript, Go' },
    });

    // Attempt nav to Education → guard dialog appears.
    fireEvent.click(screen.getByText(/education\.yml/i));
    await waitFor(() => {
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    // Click Save & switch — but the PUT will 412.
    fireEvent.click(screen.getByRole('button', { name: /save & switch/i }));

    // PUT was attempted.
    await waitFor(() => {
      expect(apiPut).toHaveBeenCalled();
    });

    // CRITICAL: the user's edits MUST still be visible — i.e. we are still
    // on the Skill tab. Education's content (with its grep token) MUST NOT
    // have rendered (which would imply the editor was unmounted).
    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript, Go')).toBeInTheDocument();
    });
    expect(screen.queryByDisplayValue(/Northeastern/i)).toBeNull();
  });

  // ── (e) window.beforeunload fires when any tab is dirty ─────────────────

  it('(e) editing Skill then firing beforeunload sets returnValue to non-empty string', async () => {
    const refetchSkill = vi.fn();
    setupSectionMock({
      skill: { data: SKILL_DATA, etag: '"etag-skill"', refetch: refetchSkill },
    });

    render(<MasterContent />);

    // Go to Skill tab
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript')).toBeInTheDocument();
    });

    // Make dirty
    fireEvent.change(screen.getByDisplayValue('Python, TypeScript'), {
      target: { value: 'Dirty before unload' },
    });

    // Wait for dirty state to register
    await waitFor(() => {
      expect(screen.getByDisplayValue('Dirty before unload')).toBeInTheDocument();
    });

    // Simulate beforeunload
    const event = new Event('beforeunload') as BeforeUnloadEvent & { returnValue: string };
    Object.defineProperty(event, 'returnValue', { writable: true, value: '' });
    const preventDefaultSpy = vi.spyOn(event, 'preventDefault');

    window.dispatchEvent(event);

    // Either preventDefault was called or returnValue was set to a non-empty string
    const guardActivated = preventDefaultSpy.mock.calls.length > 0 || event.returnValue !== '';
    expect(guardActivated).toBe(true);
  });

  // ── clean tab switch (no dirty) — no dialog ──────────────────────────────

  it('switching tabs when NOT dirty navigates immediately without dialog', async () => {
    const refetchSkill = vi.fn();
    setupSectionMock({
      skill: { data: SKILL_DATA, etag: '"etag-skill"', refetch: refetchSkill },
    });

    render(<MasterContent />);

    // Navigate to Skill tab (clean)
    fireEvent.click(screen.getByText(/skill\.yml/i));

    await waitFor(() => {
      expect(screen.getByDisplayValue('Python, TypeScript')).toBeInTheDocument();
    });

    // Navigate to Education without editing — no dialog should appear
    fireEvent.click(screen.getByText(/education\.yml/i));

    // Dialog should NOT appear
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });
});
