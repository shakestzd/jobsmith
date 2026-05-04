// API types — mirror Pydantic schemas in src/jobsmith/api/schemas/.
//
// Decision: snake_case is preserved as-is (no camelCase transform layer).
// FastAPI emits snake_case via Pydantic; matching field names verbatim keeps
// the contract trivially obvious and avoids a duplication of truth.
// The single intentional exception: `Application.updated` (Pydantic alias for
// `updated_at`) — see schemas/applications.py model_config.
//
// Slice 8 (SSE) extends this directory with `events.ts` + `useEventStream`.

// ── Applications ─────────────────────────────────────────────────────────

export type AppStatus =
  | 'queued'
  | 'gather'
  | 'draft'
  | 'review'
  | 'rendered'
  | 'running'
  | 'done'
  | 'failed';

export type AppPhase = 0 | 1 | 2 | 3;

export interface Application {
  slug: string;
  role: string | null;
  company: string | null;
  status: AppStatus | string;
  /** Serialized as `updated` via Pydantic alias (originally `updated_at`). */
  updated: string;
  phase: AppPhase | number;
  /** "pass/total", "N/N", or "—" when not yet computed. */
  anchors: string;
  /** "pass", "N flagged", or "—". */
  factcheck: string;
  /** Filenames of rendered .pdf artefacts. */
  renders: string[];
  /** Relative URL into the site (e.g. /applications/<slug>/). */
  url: string;
}

export interface ArtifactNode {
  name: string;
  path: string;
  size: number;
  /** ISO 8601 UTC timestamp. */
  mtime: string;
}

export interface ArtifactTree {
  apply_state: ArtifactNode[];
  rendered: ArtifactNode[];
}

export interface ApplicationDetail extends Application {
  artifacts: ArtifactTree;
  spec: Record<string, unknown> | null;
  prose_draft: string | null;
  cover_letter_draft: string | null;
  fact_check: Record<string, unknown> | null;
  anchor_check: Record<string, unknown> | null;
  bullet_selection: Record<string, unknown> | null;
  variables: Record<string, unknown> | null;
  config: Record<string, unknown> | null;
  truncated: boolean;
}

// ── Master ───────────────────────────────────────────────────────────────

/** Object-form work bullet (Slice A schema). */
export interface WorkDetailDict {
  bullet: string;
  anchor?: boolean | null;
  anchor_reason?: string | null;
  tags?: string[];
  drop_when?: string | null;
  [key: string]: unknown;
}

export type WorkDetail = string | WorkDetailDict;

export interface WorkEntry {
  title: string;
  /** Holds the company name (jobsmith YAML convention). */
  location: string;
  date: string;
  description: string;
  details: WorkDetail[];
  [key: string]: unknown;
}

export interface SkillEntry {
  title: string;
  description: string;
  details: string[];
  [key: string]: unknown;
}

export interface EducationEntry {
  title: string;
  location: string;
  date: string;
  description: string;
  details: string[];
  [key: string]: unknown;
}

export interface AuthorContact {
  icon?: string;
  text?: string;
  url?: string;
  [key: string]: unknown;
}

export interface AuthorName {
  first?: string;
  middle?: string;
  last?: string;
  [key: string]: unknown;
}

export interface Author {
  /** Either a string or an AuthorName-shaped dict — see schemas/master.py. */
  name?: string | AuthorName | null;
  firstname?: string | null;
  lastname?: string | null;
  address?: string;
  email?: string;
  phone?: string;
  homepage?: string;
  photo?: string;
  position?: string;
  profession?: string;
  quote?: string;
  contacts?: AuthorContact[];
  [key: string]: unknown;
}

export interface MasterPayload {
  work: WorkEntry[];
  skill: SkillEntry[];
  education: EducationEntry[];
  author: Author | null;
}
