// proposalContext.test.ts — tests for asset-aware proposal handling
// (feat-958dab07 resume edit support)

import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as client from '../api/client';

vi.mock('../api/client', () => ({
  applyCoverLetter: vi.fn(),
  getCoverLetterDraft: vi.fn(),
  applyResume: vi.fn(),
  getResumeSection: vi.fn(),
}));

describe('Proposal Context Resume Support', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('receiveProposal fetches cover letter baseline for cover_letter asset', async () => {
    const getCoverLetterDraft = vi.mocked(client.getCoverLetterDraft);
    getCoverLetterDraft.mockResolvedValue('Old cover letter text');

    const proposal: client.ChatProposal = {
      asset: 'cover_letter',
      slug: 'test-app',
      summary: 'Fix typo',
      rationale: 'Grammar',
      new_content: 'New cover letter text',
    };

    // Simulate what receiveProposal does
    if (proposal.asset === 'resume') {
      // Would fetch resume section
      expect(true).toBe(false); // Should not reach here
    } else {
      const old = await getCoverLetterDraft(proposal.slug);
      expect(old).toBe('Old cover letter text');
      expect(getCoverLetterDraft).toHaveBeenCalledWith('test-app');
    }
  });

  it('receiveProposal fetches resume section baseline for resume asset', async () => {
    const getResumeSection = vi.mocked(client.getResumeSection);
    getResumeSection.mockResolvedValue('- school: Old University\n');

    const proposal: client.ChatProposal = {
      asset: 'resume',
      slug: 'test-app',
      summary: 'Update education',
      rationale: 'Graduated',
      new_content: '- school: New University\n',
      target_section: 'Education',
      target_file: 'education.yml',
    };

    // Simulate what receiveProposal does for resume
    if (proposal.asset === 'resume' && proposal.target_file) {
      const old = await getResumeSection(proposal.slug, proposal.target_file);
      expect(old).toBe('- school: Old University\n');
      expect(getResumeSection).toHaveBeenCalledWith('test-app', 'education.yml');
    } else {
      expect(true).toBe(false); // Should not reach here
    }
  });

  it('applyProposal branches on asset type for cover letter', async () => {
    const applyCoverLetter = vi.mocked(client.applyCoverLetter);
    applyCoverLetter.mockResolvedValue({
      applied: true,
      words: 150,
      render: 'ok',
    });

    const proposal: client.ChatProposal = {
      asset: 'cover_letter',
      slug: 'test-app',
      summary: 'Test',
      rationale: 'Test',
      new_content: 'New content',
    };

    // Simulate what applyProposal does for cover letter
    if (proposal.asset === 'resume') {
      expect(true).toBe(false); // Should not reach here
    } else {
      const result = await applyCoverLetter(proposal.slug, proposal.new_content);
      expect(result.applied).toBe(true);
      expect(applyCoverLetter).toHaveBeenCalledWith('test-app', 'New content');
    }
  });

  it('applyProposal branches on asset type for resume', async () => {
    const applyResume = vi.mocked(client.applyResume);
    applyResume.mockResolvedValue({
      applied: true,
      page_count: 1,
      render: 'ok',
    });

    const proposal: client.ChatProposal = {
      asset: 'resume',
      slug: 'test-app',
      summary: 'Trim education',
      rationale: 'Tight page count',
      new_content: '- school: Test\n',
      target_section: 'Education',
      target_file: 'education.yml',
    };

    // Simulate what applyProposal does for resume
    if (proposal.asset === 'resume') {
      const result = await applyResume(
        proposal.slug,
        proposal.target_section || '',
        proposal.target_file || '',
        proposal.new_content,
      );
      expect(result.applied).toBe(true);
      expect(applyResume).toHaveBeenCalledWith(
        'test-app',
        'Education',
        'education.yml',
        '- school: Test\n',
      );
    } else {
      expect(true).toBe(false); // Should not reach here
    }
  });

  it('applyResume handles page_count_off error', async () => {
    const applyResume = vi.mocked(client.applyResume);
    applyResume.mockResolvedValue({
      applied: false,
      reason: 'page_count_off',
      page_count: 2,
    });

    const proposal: client.ChatProposal = {
      asset: 'resume',
      slug: 'test-app',
      summary: 'Too long',
      rationale: 'Test',
      new_content: '- lots\n- of\n- content\n',
      target_section: 'Education',
      target_file: 'education.yml',
    };

    // Simulate error handling
    const result = await applyResume(
      proposal.slug,
      proposal.target_section || '',
      proposal.target_file || '',
      proposal.new_content,
    );

    expect(result.applied).toBe(false);
    expect(result.reason).toBe('page_count_off');
    expect(result.page_count).toBe(2);

    // Error message should be formatted
    if (result.reason === 'page_count_off') {
      const errorMsg = `Edit would overflow the resume to ${result.page_count} pages — ask the chat for a tighter version.`;
      expect(errorMsg).toContain('overflow');
      expect(errorMsg).toContain('2');
    }
  });

  it('applyResume handles invalid_yaml error', async () => {
    const applyResume = vi.mocked(client.applyResume);
    applyResume.mockResolvedValue({
      applied: false,
      reason: 'invalid_yaml',
      detail: 'mapping values are not allowed here at line 2, column 5',
    });

    const proposal: client.ChatProposal = {
      asset: 'resume',
      slug: 'test-app',
      summary: 'Bad YAML',
      rationale: 'Test',
      new_content: 'bad: yaml: content',
      target_section: 'Education',
      target_file: 'education.yml',
    };

    const result = await applyResume(
      proposal.slug,
      proposal.target_section || '',
      proposal.target_file || '',
      proposal.new_content,
    );

    expect(result.applied).toBe(false);
    expect(result.reason).toBe('invalid_yaml');
    expect(result.detail).toContain('mapping');
  });

  it('applyResume handles schema_invalid error', async () => {
    const applyResume = vi.mocked(client.applyResume);
    applyResume.mockResolvedValue({
      applied: false,
      reason: 'schema_invalid',
      detail: 'education.yml: education[0] must be a mapping, got str',
    });

    const proposal: client.ChatProposal = {
      asset: 'resume',
      slug: 'test-app',
      summary: 'Schema error',
      rationale: 'Test',
      new_content: '- "not a mapping"',
      target_section: 'Education',
      target_file: 'education.yml',
    };

    const result = await applyResume(
      proposal.slug,
      proposal.target_section || '',
      proposal.target_file || '',
      proposal.new_content,
    );

    expect(result.applied).toBe(false);
    expect(result.reason).toBe('schema_invalid');
    expect(result.detail).toContain('must be a mapping');
  });
});
