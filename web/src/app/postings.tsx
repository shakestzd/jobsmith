// postings.tsx — Postings inbox: ranked list of sourced job postings.
//
// Features:
//  - Ranked list showing title/company/source/triage-score/rationale/age
//  - Filters: source, specialty, min score, status
//  - Actions: dismiss, queue, 'start application' (promote)
//  - Inbox badge count stored in localStorage (new-since-last-visit)

import { useState, useEffect, useCallback, useRef } from 'react';
import { Icon } from './shared';
import { usePostings } from '../api/hooks';
import { setPostingStatus, promotePosting, JobsmithApiError } from '../api/client';
import type { PostingRow, PostingPromoteResponse } from '../api/types';
import { notifyDataChanged } from '../api/client';
import SourcingHealthBanner from './SourcingHealthBanner';

// ── Constants ────────────────────────────────────────────────────────────────

const LAST_VISIT_KEY = 'jobsmith.postings.last_visit';

// ── Helpers ──────────────────────────────────────────────────────────────────

function relativeAge(iso: string): string {
  const ms = Date.now() - Date.parse(iso);
  const sec = Math.max(0, Math.round(ms / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 48) return `${hr}h`;
  return `${Math.round(hr / 24)}d`;
}

function scoreLabel(p: PostingRow): string {
  const score = p.llm_score ?? p.fast_score;
  if (score == null) return '—';
  return (score * 100).toFixed(0);
}

function scoreBg(p: PostingRow): string {
  const score = p.llm_score ?? p.fast_score;
  if (score == null) return 'var(--bg-sunk)';
  if (score >= 0.8) return 'oklch(0.92 0.08 145)';
  if (score >= 0.6) return 'oklch(0.94 0.07 80)';
  return 'var(--bg-sunk)';
}

// ── Postings View ─────────────────────────────────────────────────────────────

export interface PostingsViewProps {
  /** Called when 'start application' succeeds, with the new run's slug. */
  onPromoted?: (slug: string) => void;
}

export function PostingsView({ onPromoted }: PostingsViewProps) {
  // Filter state
  const [statusFilter, setStatusFilter] = useState<string>('sourced');
  const [sourceFilter, setSourceFilter] = useState<string>('');
  const [specialtyFilter, setSpecialtyFilter] = useState<string>('');
  const [minScore, setMinScore] = useState<string>('');

  // Per-row action state
  const [pendingId, setPendingId] = useState<number | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  // Badge: count new postings since last visit
  const [badgeCount, setBadgeCount] = useState<number>(0);
  const lastVisitRef = useRef<string>(
    localStorage.getItem(LAST_VISIT_KEY) ?? new Date(0).toISOString(),
  );

  // Build filter object for the hook
  const filter: Record<string, string | number> = {};
  if (statusFilter) filter.status = statusFilter;
  if (sourceFilter.trim()) filter.source = sourceFilter.trim();
  if (specialtyFilter.trim()) filter.specialty = specialtyFilter.trim();
  const minScoreNum = parseFloat(minScore);
  if (!isNaN(minScoreNum)) filter.min_score = minScoreNum;

  const { data: postings = [], isLoading, error } = usePostings(filter);

  // Compute badge count: postings sourced after last-visit (all statuses, no filter)
  const { data: allPostings = [] } = usePostings({ status: 'sourced' });
  useEffect(() => {
    const lastVisit = lastVisitRef.current;
    const lv = Date.parse(lastVisit);
    const count = allPostings.filter(
      (p) => Date.parse(p.first_seen_at) > lv
    ).length;
    setBadgeCount(count);
  }, [allPostings]);

  // Update last-visit on mount (so next visit resets count)
  useEffect(() => {
    localStorage.setItem(LAST_VISIT_KEY, new Date().toISOString());
  }, []);

  // ── Actions ──

  const handleDismiss = useCallback(async (p: PostingRow) => {
    setPendingId(p.id);
    setErrorMsg(null);
    try {
      await setPostingStatus(p.id, 'dismissed');
      notifyDataChanged('/api/postings');
    } catch (err) {
      setErrorMsg(err instanceof JobsmithApiError ? err.message : 'Failed to dismiss.');
    } finally {
      setPendingId(null);
    }
  }, []);

  const handleQueue = useCallback(async (p: PostingRow) => {
    setPendingId(p.id);
    setErrorMsg(null);
    try {
      await setPostingStatus(p.id, 'queued');
      notifyDataChanged('/api/postings');
    } catch (err) {
      setErrorMsg(err instanceof JobsmithApiError ? err.message : 'Failed to queue.');
    } finally {
      setPendingId(null);
    }
  }, []);

  const handlePromote = useCallback(async (p: PostingRow) => {
    setPendingId(p.id);
    setErrorMsg(null);
    setSuccessMsg(null);
    try {
      const result: PostingPromoteResponse = await promotePosting(p.id);
      notifyDataChanged('/api/postings');
      if (result.jd_fetch_failed) {
        setSuccessMsg(
          `Application started (run ${result.run_id.slice(0, 8)}…). ` +
          'Note: JD fetch failed — pipeline will proceed without cached JD text.',
        );
      } else {
        setSuccessMsg(`Application started (run ${result.run_id.slice(0, 8)}…).`);
      }
      if (result.slug && onPromoted) {
        onPromoted(result.slug);
      }
    } catch (err) {
      setErrorMsg(err instanceof JobsmithApiError ? err.message : 'Failed to promote.');
    } finally {
      setPendingId(null);
    }
  }, [onPromoted]);

  // ── Sources / specialties for filter dropdowns ──
  const { data: allForFilters = [] } = usePostings({});
  const sources = [...new Set(allForFilters.map((p) => p.source.split('/')[0]))].sort();
  const specialties = [...new Set(allForFilters.map((p) => p.specialty).filter(Boolean) as string[])].sort();

  return (
    <div className="content">
      {/* Sourcing health banner */}
      <SourcingHealthBanner />

      {/* Header */}
      <div className="page-head">
        <div>
          <h1 style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            postings inbox
            {badgeCount > 0 && (
              <span
                style={{
                  background: 'var(--accent, oklch(0.65 0.18 268))',
                  color: 'white',
                  borderRadius: 12,
                  padding: '2px 8px',
                  fontSize: 11,
                  fontFamily: 'var(--font-mono)',
                  fontWeight: 600,
                }}
              >
                {badgeCount} new
              </span>
            )}
          </h1>
          <p>sourced job postings ranked by triage score — review, queue, or start an application.</p>
        </div>
      </div>

      {/* Filters */}
      <div
        style={{
          display: 'flex',
          gap: 8,
          flexWrap: 'wrap',
          marginBottom: 16,
          alignItems: 'center',
        }}
      >
        {/* Status tabs */}
        <div
          style={{
            display: 'flex',
            border: '1px solid var(--border-strong)',
            borderRadius: 'var(--radius)',
            overflow: 'hidden',
          }}
        >
          {(['sourced', 'queued', 'dismissed', 'promoted', ''] as const).map((s) => (
            <span
              key={s || 'all'}
              style={{
                padding: '5px 10px',
                cursor: 'pointer',
                fontSize: 12,
                borderRight: s !== '' ? '1px solid var(--border)' : 'none',
                background:
                  statusFilter === s ? 'var(--bg-sunk)' : 'var(--bg-elev)',
                color:
                  statusFilter === s ? 'var(--fg)' : 'var(--fg-muted)',
              }}
              onClick={() => setStatusFilter(s)}
            >
              {s || 'all'}
            </span>
          ))}
        </div>

        {/* Source filter */}
        <select
          className="input"
          style={{ fontSize: 12, padding: '4px 8px', height: 30 }}
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
        >
          <option value="">all sources</option>
          {sources.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        {/* Specialty filter */}
        <select
          className="input"
          style={{ fontSize: 12, padding: '4px 8px', height: 30 }}
          value={specialtyFilter}
          onChange={(e) => setSpecialtyFilter(e.target.value)}
        >
          <option value="">all specialties</option>
          {specialties.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        {/* Min score */}
        <input
          className="input"
          type="number"
          min={0}
          max={1}
          step={0.05}
          placeholder="min score"
          value={minScore}
          onChange={(e) => setMinScore(e.target.value)}
          style={{ fontSize: 12, padding: '4px 8px', height: 30, width: 96 }}
        />
      </div>

      {/* Feedback messages */}
      {errorMsg && (
        <div
          style={{
            background: 'var(--danger-bg, oklch(0.97 0.03 20))',
            border: '1px solid var(--danger, oklch(0.6 0.18 20))',
            borderRadius: 'var(--radius)',
            padding: '8px 12px',
            fontSize: 12,
            marginBottom: 12,
            color: 'var(--danger, oklch(0.5 0.18 20))',
          }}
        >
          {errorMsg}
        </div>
      )}
      {successMsg && (
        <div
          style={{
            background: 'oklch(0.97 0.03 145)',
            border: '1px solid oklch(0.7 0.12 145)',
            borderRadius: 'var(--radius)',
            padding: '8px 12px',
            fontSize: 12,
            marginBottom: 12,
            color: 'oklch(0.4 0.12 145)',
          }}
        >
          {successMsg}
        </div>
      )}

      {/* Table */}
      {isLoading ? (
        <div style={{ color: 'var(--fg-subtle)', fontSize: 13, padding: 24 }}>
          loading postings…
        </div>
      ) : error ? (
        <div style={{ color: 'var(--danger, red)', fontSize: 13, padding: 24 }}>
          {error.message}
        </div>
      ) : postings.length === 0 ? (
        <div
          className="card"
          style={{ padding: 32, textAlign: 'center', color: 'var(--fg-subtle)' }}
        >
          <Icon name="inbox" size={32} style={{ marginBottom: 12, opacity: 0.3 }} />
          <div style={{ fontSize: 14 }}>no postings match these filters</div>
        </div>
      ) : (
        <div className="card" style={{ overflow: 'hidden' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: '1px solid var(--border)', background: 'var(--bg-sunk)' }}>
                <th style={thStyle}>role / company</th>
                <th style={thStyle}>source</th>
                <th style={{ ...thStyle, width: 60, textAlign: 'center' }}>triage</th>
                <th style={thStyle}>rationale</th>
                <th style={{ ...thStyle, width: 48, textAlign: 'center' }}>age</th>
                <th style={{ ...thStyle, width: 180, textAlign: 'right' }}>actions</th>
              </tr>
            </thead>
            <tbody>
              {postings.map((p) => (
                <PostingTableRow
                  key={p.id}
                  posting={p}
                  pending={pendingId === p.id}
                  onDismiss={handleDismiss}
                  onQueue={handleQueue}
                  onPromote={handlePromote}
                  scoreBg={scoreBg(p)}
                  scoreLabel={scoreLabel(p)}
                  age={relativeAge(p.first_seen_at)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Table row ─────────────────────────────────────────────────────────────────

interface PostingTableRowProps {
  posting: PostingRow;
  pending: boolean;
  onDismiss: (p: PostingRow) => void;
  onQueue: (p: PostingRow) => void;
  onPromote: (p: PostingRow) => void;
  scoreBg: string;
  scoreLabel: string;
  age: string;
}

function PostingTableRow({
  posting: p,
  pending,
  onDismiss,
  onQueue,
  onPromote,
  scoreBg,
  scoreLabel,
  age,
}: PostingTableRowProps) {
  const [expanded, setExpanded] = useState(false);

  const isPromoted = p.status === 'promoted';
  const isDismissed = p.status === 'dismissed';

  return (
    <>
      <tr
        style={{
          borderBottom: '1px solid var(--border)',
          opacity: isDismissed ? 0.45 : 1,
          cursor: 'pointer',
        }}
        onClick={() => setExpanded((v) => !v)}
      >
        {/* Role / Company */}
        <td style={tdStyle}>
          <div style={{ fontWeight: 500, color: 'var(--fg)' }}>
            {p.title ?? '—'}
          </div>
          <div style={{ color: 'var(--fg-muted)', fontSize: 12, marginTop: 1 }}>
            {p.company ?? '—'}
            {p.location && (
              <span style={{ color: 'var(--fg-subtle)', marginLeft: 6 }}>
                · {p.location}
              </span>
            )}
          </div>
        </td>

        {/* Source */}
        <td style={{ ...tdStyle, color: 'var(--fg-muted)', fontSize: 12 }}>
          {p.source}
        </td>

        {/* Triage score */}
        <td style={{ ...tdStyle, textAlign: 'center' }}>
          <span
            style={{
              display: 'inline-block',
              background: scoreBg,
              borderRadius: 4,
              padding: '2px 6px',
              fontFamily: 'var(--font-mono)',
              fontSize: 11,
              fontWeight: 600,
              minWidth: 28,
              textAlign: 'center',
            }}
          >
            {scoreLabel}
          </span>
        </td>

        {/* Rationale */}
        <td
          style={{
            ...tdStyle,
            color: 'var(--fg-muted)',
            fontSize: 12,
            maxWidth: 240,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {p.rationale ?? '—'}
        </td>

        {/* Age */}
        <td
          style={{
            ...tdStyle,
            textAlign: 'center',
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
            color: 'var(--fg-subtle)',
          }}
        >
          {age}
        </td>

        {/* Actions */}
        <td
          style={{ ...tdStyle, textAlign: 'right' }}
          onClick={(e) => e.stopPropagation()}
        >
          {!isPromoted && !isDismissed && (
            <div style={{ display: 'flex', gap: 4, justifyContent: 'flex-end' }}>
              {p.status !== 'queued' && (
                <button
                  className="btn ghost sm"
                  disabled={pending}
                  onClick={() => onQueue(p)}
                  title="queue for later"
                  style={{ fontSize: 11 }}
                >
                  queue
                </button>
              )}
              <button
                className="btn ghost sm"
                disabled={pending}
                onClick={() => onDismiss(p)}
                title="dismiss"
                style={{ fontSize: 11 }}
              >
                <Icon name="x" size={11} />
              </button>
              <button
                className="btn primary sm"
                disabled={pending}
                onClick={() => onPromote(p)}
                title="start application"
                style={{ fontSize: 11 }}
              >
                {pending ? '…' : 'apply'}
              </button>
            </div>
          )}
          {isPromoted && (
            <span
              style={{
                fontSize: 11,
                color: 'var(--fg-subtle)',
                fontFamily: 'var(--font-mono)',
              }}
            >
              promoted
            </span>
          )}
          {isDismissed && (
            <span
              style={{
                fontSize: 11,
                color: 'var(--fg-subtle)',
                fontFamily: 'var(--font-mono)',
              }}
            >
              dismissed
            </span>
          )}
        </td>
      </tr>

      {/* Expanded detail row */}
      {expanded && (
        <tr style={{ background: 'var(--bg-sunk)' }}>
          <td colSpan={6} style={{ padding: '10px 16px' }}>
            <div
              style={{
                fontSize: 12,
                color: 'var(--fg-muted)',
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
                gap: '6px 20px',
              }}
            >
              {p.specialty && (
                <div>
                  <span style={{ color: 'var(--fg-subtle)' }}>specialty: </span>
                  {p.specialty}
                </div>
              )}
              {p.comp_text && (
                <div>
                  <span style={{ color: 'var(--fg-subtle)' }}>comp: </span>
                  {p.comp_text}
                </div>
              )}
              {p.url && (
                <div style={{ gridColumn: '1 / -1' }}>
                  <span style={{ color: 'var(--fg-subtle)' }}>url: </span>
                  <a
                    href={p.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ color: 'var(--accent, oklch(0.6 0.18 268))' }}
                  >
                    {p.url.length > 80 ? p.url.slice(0, 77) + '…' : p.url}
                  </a>
                </div>
              )}
              {p.rationale && (
                <div style={{ gridColumn: '1 / -1' }}>
                  <span style={{ color: 'var(--fg-subtle)' }}>rationale: </span>
                  {p.rationale}
                </div>
              )}
              <div>
                <span style={{ color: 'var(--fg-subtle)' }}>first seen: </span>
                {p.first_seen_at.slice(0, 16).replace('T', ' ')}
              </div>
              <div>
                <span style={{ color: 'var(--fg-subtle)' }}>status: </span>
                {p.status}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ── Style constants ───────────────────────────────────────────────────────────

const thStyle: React.CSSProperties = {
  padding: '8px 12px',
  fontWeight: 500,
  fontSize: 11,
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  color: 'var(--fg-muted)',
  textAlign: 'left',
};

const tdStyle: React.CSSProperties = {
  padding: '10px 12px',
  verticalAlign: 'middle',
};

export default PostingsView;
