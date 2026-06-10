// shared.tsx — port of design/app/shared.jsx
//
// Provides the minimal stroke-icon set, badge primitives, the simple shell
// code-block highlighter, and the SAMPLE_APPS / SAMPLE_BULLETS demo fixtures
// that the dashboard / application views consume.
//
// Conventions (apply consistently in Phase 2/3):
//  - export everything as ES modules — never Object.assign(window, ...).
//  - Function components, not React.FC.
//  - Strict prop types via interface or type aliases; no `any`.
//  - Class names + DOM structure are pixel-identical to the design source.

import type { ReactNode, SVGProps } from 'react';
import type {
  AppStatus,
  BadgeKind,
  IconName,
  SampleApp,
  SampleBullet,
} from '../types';

// ── Icons (minimal stroke set) ──────────────────────────────────────────
type IconPaths = Record<IconName, ReactNode>;

const ICON_PATHS: IconPaths = {
  home: (
    <path d="M3 9.5L8 4l5 5.5V13a1 1 0 01-1 1h-2.5v-3.5h-3V14H4a1 1 0 01-1-1V9.5z" />
  ),
  plus: <path d="M8 3v10M3 8h10" />,
  folder: (
    <path d="M2 4a1 1 0 011-1h3l1 1.5h6a1 1 0 011 1V12a1 1 0 01-1 1H3a1 1 0 01-1-1V4z" />
  ),
  doc: (
    <>
      <path d="M3 2h6l4 4v8a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1z" />
      <path d="M9 2v4h4" />
    </>
  ),
  db: (
    <>
      <ellipse cx="8" cy="3.5" rx="5" ry="1.8" />
      <path d="M3 3.5V12c0 1 2.2 1.8 5 1.8s5-.8 5-1.8V3.5" />
      <path d="M3 7.5c0 1 2.2 1.8 5 1.8s5-.8 5-1.8" />
    </>
  ),
  play: <path d="M4 3l9 5-9 5V3z" />,
  check: <path d="M3 8l3 3 7-7" />,
  x: <path d="M3 3l10 10M13 3L3 13" />,
  chev: <path d="M5 4l4 4-4 4" />,
  chevd: <path d="M4 5l4 4 4-4" />,
  search: (
    <>
      <circle cx="7" cy="7" r="4" />
      <path d="M10 10l3 3" />
    </>
  ),
  bolt: <path d="M8 1L3 9h4l-1 6 6-9H8l1-5z" />,
  flag: <path d="M3 1v14M3 2h9l-2 3 2 3H3" />,
  cog: (
    <>
      <circle cx="8" cy="8" r="2.5" />
      <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3" />
    </>
  ),
  user: (
    <>
      <circle cx="8" cy="5" r="2.5" />
      <path d="M3 14c.5-2.5 2.5-4 5-4s4.5 1.5 5 4" />
    </>
  ),
  site: (
    <>
      <circle cx="8" cy="8" r="6" />
      <path d="M2 8h12M8 2c2 2 2 10 0 12M8 2c-2 2-2 10 0 12" />
    </>
  ),
  msg: (
    <path d="M2 4a1 1 0 011-1h10a1 1 0 011 1v7a1 1 0 01-1 1H6l-3 2.5V12H3a1 1 0 01-1-1V4z" />
  ),
  yaml: <path d="M2 3h12v10H2zM5 6h2M5 9h4M9 6h2" />,
  arrow: <path d="M3 8h10M9 4l4 4-4 4" />,
  dot: <circle cx="8" cy="8" r="2" />,
  sun: (
    <>
      <circle cx="8" cy="8" r="3" />
      <path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.5 1.5M11.5 11.5L13 13M3 13l1.5-1.5M11.5 4.5L13 3" />
    </>
  ),
  eye: (
    <>
      <path d="M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5z" />
      <circle cx="8" cy="8" r="2" />
    </>
  ),
  inbox: (
    <>
      <path d="M2 9h3l1.5 2h3L11 9h3M2 3h12v10H2z" />
    </>
  ),
  chart: (
    <>
      <path d="M2 12h3V8H2zM6 12h3V5H6zM10 12h3V2h-3zM1 13h14" />
    </>
  ),
};

export interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 14, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...rest}
    >
      {ICON_PATHS[name]}
    </svg>
  );
}

// ── Badge ────────────────────────────────────────────────────────────────
export interface BadgeProps {
  kind?: BadgeKind;
  dot?: boolean;
  children?: ReactNode;
}

export function Badge({ kind = 'default', dot = true, children }: BadgeProps) {
  const cls = `badge ${kind === 'default' ? '' : kind}`.trimEnd();
  return (
    <span className={cls}>
      {dot && <span className="b-dot" />}
      {children}
    </span>
  );
}

// ── StatusBadge ──────────────────────────────────────────────────────────
interface StatusEntry {
  kind: BadgeKind;
  label: string;
}

const STATUS_MAP: Record<AppStatus, StatusEntry> = {
  running: { kind: 'accent', label: 'running' },
  done: { kind: 'success', label: 'done' },
  queued: { kind: 'default', label: 'queued' },
  draft: { kind: 'warn', label: 'draft' },
  rendered: { kind: 'success', label: 'rendered' },
  incomplete: { kind: 'warn', label: 'incomplete' },
  failed: { kind: 'danger', label: 'failed' },
  gather: { kind: 'accent', label: 'gather' },
  review: { kind: 'warn', label: 'in review' },
};

export interface StatusBadgeProps {
  /** Accept any string so unknown statuses still render as a default badge. */
  status: AppStatus | string;
}

export function StatusBadge({ status }: StatusBadgeProps) {
  const entry: StatusEntry = (STATUS_MAP as Record<string, StatusEntry>)[
    status
  ] ?? { kind: 'default', label: status };
  return <Badge kind={entry.kind}>{entry.label}</Badge>;
}

// ── Code (very small shell highlighter) ──────────────────────────────────
export interface CodeProps {
  children?: ReactNode;
  /** Reserved for future per-language colouring; currently shell-only. */
  lang?: string;
}

export function Code({ children }: CodeProps) {
  const lines = String(children ?? '').split('\n');
  return (
    <pre className="code">
      {lines.map((line, i) => {
        const html = line
          .replace(/(#.*)$/g, '<span class="c">$1</span>')
          .replace(/(\$|❯)\s/g, '<span class="k">$&</span>')
          .replace(/(--?[a-z-]+)/g, '<span class="n">$1</span>')
          .replace(/('[^']+'|"[^"]+")/g, '<span class="s">$&</span>');
        return (
          <div
            key={i}
            dangerouslySetInnerHTML={{ __html: html || '&nbsp;' }}
          />
        );
      })}
    </pre>
  );
}

// ── Sample data ──────────────────────────────────────────────────────────
export const SAMPLE_APPS: SampleApp[] = [
  {
    slug: 'anthropic-applied-ai-2026-04',
    role: 'Member of Technical Staff, Applied AI',
    company: 'Anthropic',
    status: 'rendered',
    updated: '12 min ago',
    phase: 3,
    anchors: '14/14',
    factcheck: 'pass',
    renders: ['resume.pdf', 'cover.pdf'],
    url: 'https://www.anthropic.com/jobs/...',
  },
  {
    slug: 'vercel-platform-eng-2026-04',
    role: 'Platform Engineer',
    company: 'Vercel',
    status: 'review',
    updated: '1 hr ago',
    phase: 3,
    anchors: '12/12',
    factcheck: 'pass',
    renders: ['resume.pdf', 'cover.pdf'],
    url: 'https://vercel.com/careers/...',
  },
  {
    slug: 'linear-product-eng-2026-04',
    role: 'Product Engineer',
    company: 'Linear',
    status: 'running',
    updated: 'just now',
    phase: 2,
    anchors: '—',
    factcheck: '—',
    renders: [],
    url: 'https://linear.app/careers/...',
  },
  {
    slug: 'stripe-infra-2026-03',
    role: 'Infrastructure Engineer',
    company: 'Stripe',
    status: 'draft',
    updated: '3 hrs ago',
    phase: 2,
    anchors: '11/12',
    factcheck: '1 flagged',
    renders: [],
    url: 'https://stripe.com/jobs/...',
  },
  {
    slug: 'resend-developer-experience-2026-03',
    role: 'Developer Experience',
    company: 'Resend',
    status: 'rendered',
    updated: 'yesterday',
    phase: 3,
    anchors: '9/9',
    factcheck: 'pass',
    renders: ['resume.pdf', 'cover.pdf'],
    url: 'https://resend.com/careers/...',
  },
  {
    slug: 'fly-systems-2026-03',
    role: 'Systems Engineer',
    company: 'fly.io',
    status: 'failed',
    updated: '2 days ago',
    phase: 1,
    anchors: '—',
    factcheck: '—',
    renders: [],
    url: 'https://fly.io/jobs/...',
  },
  {
    slug: 'render-cli-2026-03',
    role: 'CLI / Tooling Engineer',
    company: 'Render',
    status: 'rendered',
    updated: '4 days ago',
    phase: 3,
    anchors: '10/10',
    factcheck: 'pass',
    renders: ['resume.pdf', 'cover.pdf'],
    url: 'https://render.com/careers/...',
  },
  {
    slug: 'val-town-frontend-2026-03',
    role: 'Frontend Engineer',
    company: 'Val Town',
    status: 'queued',
    updated: 'just now',
    phase: 0,
    anchors: '—',
    factcheck: '—',
    renders: [],
    url: 'https://val.town/jobs/...',
  },
];

export const SAMPLE_BULLETS: SampleBullet[] = [
  {
    id: 'b1',
    anchor: true,
    text: 'Led the rebuild of the deploy pipeline, cutting median deploy time from 11m to 2m20s and eliminating the manual approval bottleneck for 90% of services.',
  },
  {
    id: 'b2',
    anchor: true,
    text: 'Designed and shipped the artifact-cache layer (Rust + S3) that now serves 1.2B requests/month with p99 < 38ms.',
  },
  {
    id: 'b3',
    anchor: false,
    text: 'Wrote the team’s onboarding doc; mentored 4 new engineers through their first production landing.',
  },
  {
    id: 'b4',
    anchor: true,
    text: 'Drove the migration of 320 services off the legacy scheduler; recovered ~$140k/yr in idle compute.',
  },
  {
    id: 'b5',
    anchor: false,
    text: 'Owned weekly on-call rotation; reduced page volume 42% by killing two noisy alerting paths.',
  },
  {
    id: 'b6',
    anchor: false,
    text: 'Contributed to internal Rust style guide; wrote three RFCs accepted by infra council.',
  },
  {
    id: 'b7',
    anchor: true,
    text: 'Built the live-reload dev environment used by ~180 engineers; cut cold-start from 18s to 3s.',
  },
  {
    id: 'b8',
    anchor: false,
    text: 'Paired with the security team on the secrets-rotation rewrite; landed in Q2.',
  },
];
