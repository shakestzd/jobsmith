// funnel.tsx — Sourcing-to-apply funnel dashboard view.
//
// Shows per-stage counts (sourced → queued → promoted → interview → offer),
// adjacent-stage conversion rates, and per-source yield.
// Window filter: 7d / 30d / 90d / all.

import { useState, useEffect } from 'react';
import type { FunnelResponse } from '../api/types';
import type { FunnelWindow } from '../api/client';
import { getFunnel } from '../api/client';

// ── Helpers ──────────────────────────────────────────────────────────────

function pct(rate: number | null): string {
  if (rate === null) return '—';
  return `${Math.round(rate * 100)}%`;
}

// ── FunnelView ────────────────────────────────────────────────────────────

const WINDOWS: { label: string; value: FunnelWindow }[] = [
  { label: '7d', value: 7 },
  { label: '30d', value: 30 },
  { label: '90d', value: 90 },
  { label: 'all', value: 'all' },
];

export function FunnelView() {
  const [window, setWindow] = useState<FunnelWindow>(30);
  const [data, setData] = useState<FunnelResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    getFunnel(window)
      .then((d) => {
        setData(d);
        setLoading(false);
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
  }, [window]);

  const handleWindowChange = (w: FunnelWindow) => {
    setWindow(w);
  };

  if (loading) {
    return (
      <div className="content">
        <div style={{ color: 'var(--fg-muted)', padding: 24 }}>loading funnel…</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="content">
        <div style={{ color: 'var(--danger, #c0392b)', padding: 24 }}>Error: {error}</div>
      </div>
    );
  }

  const stages = data?.stages;
  const conversions = data?.conversions;
  const perSource = data?.per_source ?? [];
  const isEmpty = !stages || stages.sourced === 0;

  return (
    <div className="content">
      {/* Header */}
      <div className="page-head" style={{ marginBottom: 20 }}>
        <div>
          <h1 style={{ margin: 0 }}>Funnel</h1>
          <div style={{ color: 'var(--fg-muted)', fontSize: 13, marginTop: 4 }}>
            Sourcing-to-offer conversion dashboard
          </div>
        </div>
        {/* Window filter */}
        <div className="tabs" style={{ marginBottom: 0 }}>
          {WINDOWS.map((w) => (
            <div
              key={String(w.value)}
              className={`tab ${window === w.value ? 'active' : ''}`}
              onClick={() => handleWindowChange(w.value)}
            >
              {w.label}
            </div>
          ))}
        </div>
      </div>

      {isEmpty ? (
        <div className="card" style={{ padding: 24, color: 'var(--fg-muted)', textAlign: 'center' }}>
          No postings in this window. Source some jobs or expand the time range.
        </div>
      ) : (
        <>
          {/* Stage funnel */}
          <div className="card" style={{ padding: '20px 24px', marginBottom: 16 }}>
            <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 16, color: 'var(--fg-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Pipeline stages
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 12 }}>
              {[
                { label: 'sourced', count: stages!.sourced },
                { label: 'queued', count: stages!.queued },
                { label: 'promoted', count: stages!.promoted },
                { label: 'interview', count: stages!.interview },
                { label: 'offer', count: stages!.offer },
              ].map((s) => (
                <div key={s.label} style={{ textAlign: 'center', padding: '12px 8px', borderRadius: 6, background: 'var(--bg-subtle, rgba(0,0,0,0.03))' }}>
                  <div style={{ fontSize: 28, fontWeight: 700, fontFamily: 'var(--font-mono)', lineHeight: 1 }}>
                    {s.count}
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--fg-muted)', marginTop: 6 }}>
                    {s.label}
                  </div>
                </div>
              ))}
            </div>

            {/* Conversion rates between adjacent stages */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginTop: 12 }}>
              {[
                { label: 'sourced → queued', rate: conversions!.sourced_to_queued },
                { label: 'queued → promoted', rate: conversions!.queued_to_promoted },
                { label: 'promoted → interview', rate: conversions!.promoted_to_interview },
                { label: 'interview → offer', rate: conversions!.interview_to_offer },
              ].map((c) => (
                <div key={c.label} style={{ textAlign: 'center', padding: '8px', borderRadius: 6, background: 'var(--bg-subtle, rgba(0,0,0,0.02))' }}>
                  <div style={{ fontSize: 20, fontWeight: 600, fontFamily: 'var(--font-mono)', color: c.rate !== null ? 'var(--accent, oklch(0.6 0.18 268))' : 'var(--fg-subtle)' }}>
                    {pct(c.rate)}
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--fg-muted)', marginTop: 4 }}>
                    {c.label}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Per-source yield table */}
          {perSource.length > 0 && (
            <div className="card" style={{ padding: '20px 24px' }}>
              <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 12, color: 'var(--fg-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                Per-source yield
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                <thead>
                  <tr style={{ borderBottom: '1px solid var(--border)' }}>
                    {['Source', 'Postings', 'Applied', 'Interview', 'Offer'].map((h) => (
                      <th key={h} style={{ textAlign: h === 'Source' ? 'left' : 'right', padding: '6px 10px', color: 'var(--fg-muted)', fontWeight: 500 }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {perSource.map((row) => (
                    <tr key={row.source} style={{ borderBottom: '1px solid var(--border-subtle, rgba(0,0,0,0.06))' }}>
                      <td style={{ padding: '8px 10px', fontFamily: 'var(--font-mono)', fontSize: 12 }}>{row.source}</td>
                      <td style={{ padding: '8px 10px', textAlign: 'right' }}>{row.postings}</td>
                      <td style={{ padding: '8px 10px', textAlign: 'right' }}>{row.applied}</td>
                      <td style={{ padding: '8px 10px', textAlign: 'right' }}>{row.interview}</td>
                      <td style={{ padding: '8px 10px', textAlign: 'right' }}>{row.offer}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
