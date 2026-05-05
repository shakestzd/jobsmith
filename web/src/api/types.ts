// types.ts — shapes returned by the jobsmith FastAPI master + applications APIs.
//
// Derived from the Pydantic models in src/jobsmith/api/schemas/. We intentionally
// keep these loose (extra: "allow" on the server side) and lean on `unknown` /
// optional fields rather than over-specifying. Refine in follow-up slices when
// concrete UI needs emerge.

// ── Master · work.yml ────────────────────────────────────────────────────

/** Object-form work bullet (Slice A schema). */
export interface MasterWorkDetailDict {
  bullet: string;
  anchor?: boolean | null;
  anchor_reason?: string | null;
  tags?: string[];
  drop_when?: string | null;
  [k: string]: unknown;
}

export type MasterWorkDetail = string | MasterWorkDetailDict;

/** One position entry from work.yml. `location` holds the company name. */
export interface MasterWorkRole {
  title: string;
  location?: string;
  date?: string;
  description?: string;
  details?: MasterWorkDetail[];
  [k: string]: unknown;
}

// ── Master · skill.yml ───────────────────────────────────────────────────

export interface MasterSkillGroup {
  title: string;
  description?: string;
  details?: string[];
  [k: string]: unknown;
}

// ── Master · education.yml ───────────────────────────────────────────────

export interface MasterEducationEntry {
  title: string;
  location?: string;
  date?: string;
  description?: string;
  details?: string[];
  [k: string]: unknown;
}

// ── Master · author.yml ──────────────────────────────────────────────────

export interface MasterAuthorContact {
  icon?: string;
  text?: string;
  url?: string;
  [k: string]: unknown;
}

export interface MasterAuthor {
  name?: unknown; // str OR { first, middle, last }
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
  contacts?: MasterAuthorContact[];
  [k: string]: unknown;
}

// ── Master · benchmark.md ────────────────────────────────────────────────

export interface MasterBenchmark {
  text: string;
  /** SHA-256 hex digest; "" when file absent. Used as If-Match for concurrent writes. */
  version: string;
}

// ── Master · combined payload ────────────────────────────────────────────

export interface MasterPayload {
  work: MasterWorkRole[];
  skill: MasterSkillGroup[];
  education: MasterEducationEntry[];
  author: MasterAuthor | null;
}

// Section keys accepted by /api/master/{section} GET/PUT.
export type MasterSectionName = 'work' | 'skill' | 'education' | 'author';

/** Discriminated mapping section → response shape. */
export interface MasterSectionData {
  work: MasterWorkRole[];
  skill: MasterSkillGroup[];
  education: MasterEducationEntry[];
  author: MasterAuthor | null;
  benchmark: MasterBenchmark;
}

// ── Applications ─────────────────────────────────────────────────────────

/** Summary row from /api/applications. */
export interface ApplicationRow {
  slug: string;
  run_id: string;
  phase: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  role: string | null;
  company: string | null;
}

/** One specialist artifact envelope (from /api/applications/{slug}.artifacts[]). */
export interface ApplicationArtifact {
  run_id: string;
  specialist: string;
  kind: string;
  output: Record<string, unknown>;
  finished_at: string | null;
  transcript_ref: string | null;
  version?: number;
}

export interface ApplicationDetail extends ApplicationRow {
  artifacts: ApplicationArtifact[];
}

// ── Write responses ──────────────────────────────────────────────────────

export interface WriteResponse {
  section: MasterSectionName;
  path: string;
  bytes_written: number;
}

// ── Config ───────────────────────────────────────────────────────────────

export interface ConfigMasterPaths {
  work_yml: string;
  skill_yml: string;
  education_yml: string;
  author_yml: string;
  publication_yml?: string | null;
  award_yml?: string | null;
  projects_yml?: string | null;
  [k: string]: unknown;
}

export interface ConfigOutputPaths {
  applications_dir: string;
  job_search_db: string;
  jobsmith_db: string;
  review_db_dir: string;
  [k: string]: unknown;
}

export interface ConfigUserIdentity {
  name: string;
  email: string;
  phone: string;
  location: string;
  github: string;
  linkedin: string;
  [k: string]: unknown;
}

export interface JobsmithConfig {
  master: ConfigMasterPaths;
  output: ConfigOutputPaths;
  user: ConfigUserIdentity;
  voice: Record<string, unknown>;
  anchor_thresholds: Record<string, unknown>;
  cover_letter: Record<string, unknown>;
  resume: Record<string, unknown>;
  fit_scorer: Record<string, unknown>;
  portfolio: Record<string, unknown>;
  benchmarks: Record<string, unknown>;
  [k: string]: unknown;
}

export interface ValidateResponse {
  ok: boolean;
  errors: string[];
}
