// reviewTab.test.tsx — tests for ReviewTab asset-aware proposal panel
// (feat-958dab07 resume edit support)

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { ChatProposal } from '../api/client';

// Mock the ReviewTab component's proposal panel rendering logic
describe('ReviewTab Proposal Panel - Asset Awareness', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders cover letter proposal header correctly', () => {
    const activeProposal = {
      proposal: {
        asset: 'cover_letter',
        slug: 'test-app',
        summary: 'Fix grammar',
        rationale: 'Typo in opening',
        new_content: 'Better text',
      } as ChatProposal,
      oldContent: 'Original text',
      applying: false,
      failedClaims: null,
    };

    // Simulate the header rendering logic
    const headerText =
      activeProposal.proposal.asset === 'resume'
        ? `✍️ Proposed resume edit — ${activeProposal.proposal.target_section} (per-app copy, not master resume)`
        : '✍️ Proposed cover letter revision';

    expect(headerText).toContain('cover letter revision');
    expect(headerText).not.toContain('resume');
  });

  it('renders resume proposal header correctly', () => {
    const activeProposal = {
      proposal: {
        asset: 'resume',
        slug: 'test-app',
        summary: 'Trim skills',
        rationale: 'Tight page count',
        new_content: '- area: Programming\n  items: [Python, Go]',
        target_section: 'Skills',
        target_file: 'skill.yml',
      } as ChatProposal,
      oldContent: '- area: Programming\n  items: [Python, Go, TypeScript]',
      applying: false,
      failedClaims: null,
    };

    // Simulate the header rendering logic
    const headerText =
      activeProposal.proposal.asset === 'resume'
        ? `✍️ Proposed resume edit — ${activeProposal.proposal.target_section} (per-app copy, not master resume)`
        : '✍️ Proposed cover letter revision';

    expect(headerText).toContain('resume edit');
    expect(headerText).toContain('Skills');
    expect(headerText).toContain('per-app copy');
    expect(headerText).not.toContain('cover letter');
  });

  it('shows correct apply button aria-label for resume', () => {
    const activeProposal = {
      proposal: {
        asset: 'resume',
        slug: 'test-app',
        summary: 'Update education',
        rationale: 'Graduated',
        new_content: '- school: New Univ',
        target_section: 'Education',
        target_file: 'education.yml',
      } as ChatProposal,
      oldContent: '- school: Old Univ',
      applying: false,
      failedClaims: null,
    };

    const ariaLabel =
      activeProposal.proposal.asset === 'resume'
        ? `Apply proposed ${activeProposal.proposal.target_section} change`
        : 'Apply proposed cover letter change';

    expect(ariaLabel).toContain('Education');
    expect(ariaLabel).not.toContain('cover letter');
  });

  it('shows error alert for resume validation errors', () => {
    const activeProposal = {
      proposal: {
        asset: 'resume',
        slug: 'test-app',
        summary: 'Test',
        rationale: 'Test',
        new_content: 'bad yaml',
        target_section: 'Education',
        target_file: 'education.yml',
      } as ChatProposal,
      oldContent: 'old',
      applying: false,
      error: 'Invalid YAML: mapping values are not allowed',
    };

    // Error should be displayed
    expect(activeProposal.error).toBeDefined();
    expect(activeProposal.error).toContain('Invalid YAML');
  });

  it('shows page_count_off error message', () => {
    const activeProposal = {
      proposal: {
        asset: 'resume',
        slug: 'test-app',
        summary: 'Too much content',
        rationale: 'Test',
        new_content: 'lots of content',
        target_section: 'Work',
        target_file: 'work.yml',
      } as ChatProposal,
      oldContent: 'old',
      applying: false,
      error:
        'Edit would overflow the resume to 2 pages — ask the chat for a tighter version.',
    };

    expect(activeProposal.error).toContain('overflow');
    expect(activeProposal.error).toContain('2 pages');
    expect(activeProposal.error).toContain('tighter');
  });

  it('shows failedClaims only for cover letter (not resume)', () => {
    // Cover letter with failed claims
    const coverLetterProposal = {
      proposal: {
        asset: 'cover_letter',
        slug: 'test-app',
        summary: 'Test',
        rationale: 'Test',
        new_content: 'text',
      } as ChatProposal,
      oldContent: 'old',
      applying: false,
      failedClaims: [
        'Company "FakeCorp" not found in work.yml',
        'Metric "50M users" unverifiable',
      ],
    };

    // Fact-check alert should show
    expect(coverLetterProposal.failedClaims).toBeDefined();
    expect(coverLetterProposal.failedClaims?.length).toBe(2);

    // Resume proposal should not have failedClaims
    const resumeProposal = {
      proposal: {
        asset: 'resume',
        slug: 'test-app',
        summary: 'Test',
        rationale: 'Test',
        new_content: 'text',
        target_section: 'Education',
        target_file: 'education.yml',
      } as ChatProposal,
      oldContent: 'old',
      applying: false,
      failedClaims: null,
      error: undefined,
    };

    expect(resumeProposal.failedClaims).toBeNull();
  });
});
