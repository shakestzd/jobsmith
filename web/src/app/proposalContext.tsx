// proposalContext.tsx — shared state for chat-proposed cover-letter and resume edits
// (feat-22fad04f, feat-958dab07) — moved out of chat.tsx so ReviewTab can read + act on it.

import { createContext, useContext, useState, useCallback, useRef } from 'react';
import type { ReactNode } from 'react';
import {
  applyCoverLetter,
  getCoverLetterDraft,
  applyResume,
  getResumeSection,
} from '../api/client';
import type { ChatProposal } from '../api/client';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PendingProposal {
  proposal: ChatProposal;
  oldContent: string;        // OLD diff side (cover letter or resume section)
  applying: boolean;
  failedClaims: string[] | null; // populated on 422 fact-check rejection (cover letter only)
  error?: string;            // populated on 422 for resume (page_count_off, invalid_yaml, schema_invalid)
}

interface ProposalContextValue {
  pendingProposal: PendingProposal | null;
  /** Set a new pending proposal (called from chat on SSE proposal event). */
  setPendingProposal: (p: PendingProposal | null) => void;
  /** Fetch the old content and set the proposal atomically. */
  receiveProposal: (prop: ChatProposal) => void;
  /** Apply the pending proposal to the server. Returns true on success. */
  applyProposal: () => Promise<boolean>;
  /** Discard the pending proposal without applying. */
  rejectProposal: () => void;
  /**
   * Bumped by a successful apply so ReviewTab can trigger a data refresh.
   * Consumers should include this in their useEffect dependency arrays.
   */
  appliedVersion: number;
  /** Called from ReviewTab on success to push an assistant note into chat. */
  onApplied: ((note: string) => void) | null;
  setOnApplied: (fn: ((note: string) => void) | null) => void;
}

// ---------------------------------------------------------------------------
// Context + Provider
// ---------------------------------------------------------------------------

const ProposalContext = createContext<ProposalContextValue | null>(null);

export function ProposalProvider({ children }: { children: ReactNode }) {
  const [pendingProposal, setPendingProposal] = useState<PendingProposal | null>(null);
  const [appliedVersion, setAppliedVersion] = useState(0);
  // We store the chat's "push assistant note" callback so the context can
  // inject a confirmation message even when the action is triggered from ReviewTab.
  const onAppliedRef = useRef<((note: string) => void) | null>(null);
  const [, forceOnAppliedRender] = useState(0);

  const setOnApplied = useCallback((fn: ((note: string) => void) | null) => {
    onAppliedRef.current = fn;
    forceOnAppliedRender(v => v + 1);
  }, []);

  const receiveProposal = useCallback((prop: ChatProposal) => {
    setPendingProposal({
      proposal: prop,
      oldContent: '',
      applying: false,
      failedClaims: null,
      error: undefined,
    });
    // Fetch OLD content lazily based on asset type.
    const fetchOld =
      prop.asset === 'resume' && prop.target_file
        ? getResumeSection(prop.slug, prop.target_file)
        : getCoverLetterDraft(prop.slug);

    void fetchOld
      .then((old) =>
        setPendingProposal((cur) =>
          cur && cur.proposal === prop ? { ...cur, oldContent: old } : cur,
        ),
      )
      .catch(() => {
        /* leave oldContent empty — diff still shows additions */
      });
  }, []);

  const applyProposal = useCallback(async (): Promise<boolean> => {
    if (!pendingProposal || pendingProposal.applying) return false;
    const { proposal } = pendingProposal;
    setPendingProposal((cur) =>
      cur ? { ...cur, applying: true, failedClaims: null, error: undefined } : cur,
    );
    try {
      if (proposal.asset === 'resume') {
        // Resume proposal
        const result = await applyResume(
          proposal.slug,
          proposal.target_section || '',
          proposal.target_file || '',
          proposal.new_content,
        );
        if (result.applied) {
          const renderNote =
            result.render && result.render !== 'ok' ? ` (render: ${result.render})` : '';
          onAppliedRef.current?.(
            `✓ Applied — ${proposal.target_section} section updated${renderNote}.`,
          );
          setPendingProposal(null);
          setAppliedVersion((v) => v + 1);
          return true;
        } else {
          // 422 errors: page_count_off, invalid_yaml, schema_invalid
          let errorMsg = result.reason || 'Unknown error';
          if (result.reason === 'page_count_off') {
            errorMsg = `Edit would overflow the resume to ${result.page_count} pages — ask the chat for a tighter version.`;
          } else if (
            result.reason === 'invalid_yaml' ||
            result.reason === 'schema_invalid'
          ) {
            errorMsg = result.detail || errorMsg;
          }
          setPendingProposal((cur) =>
            cur ? { ...cur, applying: false, error: errorMsg } : cur,
          );
          return false;
        }
      } else {
        // Cover letter proposal (asset === 'cover_letter')
        const result = await applyCoverLetter(proposal.slug, proposal.new_content);
        if (result.applied) {
          const renderNote =
            result.render && result.render !== 'ok' ? ` (render: ${result.render})` : '';
          onAppliedRef.current?.(`✓ Applied — cover letter updated${renderNote}.`);
          setPendingProposal(null);
          setAppliedVersion((v) => v + 1);
          return true;
        } else {
          // 422 fact-check failure — keep the card so the user can ask for a fix.
          setPendingProposal((cur) =>
            cur ? { ...cur, applying: false, failedClaims: result.failed_claims ?? [] } : cur,
          );
          return false;
        }
      }
    } catch {
      setPendingProposal((cur) =>
        cur ? { ...cur, applying: false, error: 'Apply failed' } : cur,
      );
      return false;
    }
  }, [pendingProposal]);

  const rejectProposal = useCallback(() => {
    setPendingProposal(null);
  }, []);

  return (
    <ProposalContext.Provider
      value={{
        pendingProposal,
        setPendingProposal,
        receiveProposal,
        applyProposal,
        rejectProposal,
        appliedVersion,
        onApplied: onAppliedRef.current,
        setOnApplied,
      }}
    >
      {children}
    </ProposalContext.Provider>
  );
}

export function useProposal(): ProposalContextValue {
  const ctx = useContext(ProposalContext);
  if (!ctx) throw new Error('useProposal must be used inside ProposalProvider');
  return ctx;
}
