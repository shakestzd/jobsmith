// SourcingHealthBanner.tsx — Sourcing pipeline health banner
//
// Displays status of the sourcing/run-health endpoint:
// - ok: renders nothing (hidden)
// - failed: "Last sourcing run FAILED"
// - degraded: "Last sourcing run DEGRADED — sources: [list]"
// - stale: "Last sourcing run is STALE (25+ hours old)"
// - no_runs: "No sourcing runs yet"
// - unknown: "Unable to check sourcing health"

import { useRunHealth } from '../api/hooks';

export interface SourcingHealthBannerProps {
  /** Optional CSS class for styling. */
  className?: string;
}

interface RunHealthResponse {
  state: 'ok' | 'failed' | 'degraded' | 'stale' | 'no_runs' | 'unknown';
  last_run_id?: string | null;
  last_run_status?: string | null;
  finished_at?: string | null;
  error?: string | null;
  degraded_sources?: string[] | null;
  age_hours?: number | null;
}

/**
 * SourcingHealthBanner: Renders a visible alert when sourcing health is not ok.
 * Fetches from GET /api/sourcing/run-health and displays warnings for:
 * - failed: sourcing pipeline ran but returned failed status
 * - degraded: some sources had errors
 * - stale: last successful run is >25 hours old
 * - no_runs: sourcing_runs table is empty
 * - unknown: DB not found or query failed
 *
 * When state is 'ok', renders nothing.
 */
export function SourcingHealthBanner({ className = '' }: SourcingHealthBannerProps) {
  const { data, isLoading, error } = useRunHealth();

  // While loading or on network error, show nothing (non-blocking).
  if (isLoading || error) {
    return null;
  }

  if (!data) {
    return null;
  }

  const state = data.state as keyof typeof bannerConfig;
  const config = bannerConfig[state];

  // ok state renders nothing
  if (!config) {
    return null;
  }

  return (
    <div
      className={className}
      style={{
        background: config.backgroundColor,
        border: `1px solid ${config.borderColor}`,
        borderRadius: 'var(--radius)',
        padding: '12px 16px',
        fontSize: 13,
        marginBottom: 16,
        color: config.textColor,
      }}
    >
      <div style={{ fontWeight: 500, marginBottom: 4 }}>
        {config.title}
      </div>
      {config.message(data as RunHealthResponse) && (
        <div style={{ fontSize: 12, color: config.subtextColor, marginTop: 4 }}>
          {config.message(data as RunHealthResponse)}
        </div>
      )}
    </div>
  );
}

interface BannerConfig {
  backgroundColor: string;
  borderColor: string;
  textColor: string;
  subtextColor: string;
  title: string;
  message: (data: RunHealthResponse) => string | null;
}

const bannerConfig: Record<string, BannerConfig> = {
  failed: {
    backgroundColor: 'var(--danger-bg, oklch(0.97 0.03 20))',
    borderColor: 'var(--danger, oklch(0.6 0.18 20))',
    textColor: 'var(--danger, oklch(0.5 0.18 20))',
    subtextColor: 'var(--danger, oklch(0.45 0.15 20))',
    title: 'Last sourcing run FAILED',
    message: (data) => {
      if (data.error) {
        return `Error: ${data.error}`;
      }
      return null;
    },
  },
  degraded: {
    backgroundColor: 'oklch(0.97 0.04 40)',
    borderColor: 'oklch(0.7 0.12 40)',
    textColor: 'oklch(0.5 0.12 40)',
    subtextColor: 'oklch(0.45 0.1 40)',
    title: 'Last sourcing run DEGRADED',
    message: (data) => {
      if (data.degraded_sources && data.degraded_sources.length > 0) {
        return `Sources with errors: ${data.degraded_sources.join(', ')}`;
      }
      return null;
    },
  },
  stale: {
    backgroundColor: 'oklch(0.97 0.04 80)',
    borderColor: 'oklch(0.7 0.12 80)',
    textColor: 'oklch(0.5 0.12 80)',
    subtextColor: 'oklch(0.45 0.1 80)',
    title: 'Last sourcing run is STALE',
    message: (data) => {
      if (data.age_hours !== null && data.age_hours !== undefined) {
        return `Last successful run was ${data.age_hours.toFixed(1)} hours ago (>25h threshold).`;
      }
      return null;
    },
  },
  no_runs: {
    backgroundColor: 'oklch(0.97 0.04 80)',
    borderColor: 'oklch(0.7 0.12 80)',
    textColor: 'oklch(0.5 0.12 80)',
    subtextColor: 'oklch(0.45 0.1 80)',
    title: 'No sourcing runs yet',
    message: () => 'The sourcing pipeline has not completed a run. Check back soon.',
  },
  unknown: {
    backgroundColor: 'oklch(0.97 0.04 80)',
    borderColor: 'oklch(0.7 0.12 80)',
    textColor: 'oklch(0.5 0.12 80)',
    subtextColor: 'oklch(0.45 0.1 80)',
    title: 'Unable to check sourcing health',
    message: () => 'Could not determine the status of the sourcing pipeline.',
  },
};

export default SourcingHealthBanner;
