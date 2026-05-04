// API types — mirror Pydantic schemas in src/jobsmith/api/schemas/.
//
// Decision: snake_case is preserved as-is (no camelCase transform layer).
// FastAPI emits snake_case via Pydantic; matching field names verbatim keeps
// the contract trivially obvious and avoids a duplication of truth.
//
// Rebuilt for PR #29 API surface:
//   - Applications/ApplicationDetail now reflect DB-backed schema (run_id,
//     phase, status, started_at, finished_at) — no longer the FS-derived
//     rich summary (role, company, renders, anchors, factcheck, url).
//   - ArtifactEnvelope is the unified artifact model (specialist_outputs row).
//   - BenchmarkResponse added for /api/master/benchmark.
//   - MasterSection union type added for hook inference.

// ── Applications ─────────────────────────────────────────────────────────

/** Summary row from apply_runs — one entry per unique slug (latest run). */
export interface Application {
  slug: string;
  run_id: string;
  phase: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
}

/** Full application detail: latest run metadata + latest artifacts. */
export interface ApplicationDetail extends Application {
  artifacts: ArtifactEnvelope[];
}

// ── Artifacts ────────────────────────────────────────────────────────────

/** One specialist output row, deserialised for API consumers. */
export interface ArtifactEnvelope {
  run_id: string;
  specialist: string;
  kind: string;
  output: Record<string, unknown>;
  finished_at: string | null;
  transcript_ref: string | null;
  version: number;
}

/** Request body for PUT /api/applications/{slug}/runs/{run_id}/artifacts/{kind}. */
export interface PutArtifactBody {
  output: Record<string, unknown>;
  specialist?: string;
  transcript_ref?: string | null;
  finished_at?: string | null;
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

/** Union of the four updatable master section names. */
export type MasterSection = 'work' | 'skill' | 'education' | 'author';

/** Per-section content types for write operations. */
export type MasterSectionPayload<S extends MasterSection> =
  S extends 'work' ? WorkEntry[] :
  S extends 'skill' ? SkillEntry[] :
  S extends 'education' ? EducationEntry[] :
  S extends 'author' ? Author | { author: Author[] } :
  never;

/** Response body for PUT /api/master/{section}. */
export interface MasterWriteResponse {
  section: MasterSection;
  path: string;
  bytes_written: number;
}

// ── Benchmark ────────────────────────────────────────────────────────────

/** Response body for GET/PUT /api/master/benchmark. */
export interface BenchmarkResponse {
  /** Raw markdown text of benchmark.md (empty string when file absent). */
  text: string;
  /** SHA-256 hex digest of text content; "" when file absent. */
  version: string;
}

/** Request body for PUT /api/master/benchmark. */
export interface BenchmarkPayload {
  text: string;
}

// ── Snapshots ────────────────────────────────────────────────────────────

/** Request body for POST /api/applications/{slug}/runs/{run_id}/snapshot. */
export interface SnapshotRequest {
  [key: string]: unknown;
}

/** Response body for POST .../snapshot. */
export interface SnapshotResponse {
  [key: string]: unknown;
}

// ── Events (SSE types — see events.ts for the hook) ──────────────────────

export type Verbosity = 'quiet' | 'normal' | 'verbose';

export interface PhaseEventData {
  run_id: string;
  phase: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  rowid: number;
}

export interface SpecialistEventData {
  run_id: string;
  specialist: string;
  kind: string;
  phase: string | null;
  status: string | null;
  finished_at: string | null;
  transcript_ref: string | null;
  rowid: number;
}

export interface LogEventData {
  stream: 'stdout' | 'stderr';
  line: string;
  timestamp: string;
  run_id: string;
}

export type SpecialistEvent =
  | { kind: 'phase'; data: PhaseEventData; receivedAt: string }
  | { kind: 'specialist'; data: SpecialistEventData; receivedAt: string }
  | { kind: 'log'; data: LogEventData; receivedAt: string };
