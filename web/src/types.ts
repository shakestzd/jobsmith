// Shared TypeScript types for the jobsmith web app.
//
// Phase 1 establishes these shapes so Phase 2/3 modules import a stable
// vocabulary. Add new types here rather than re-declaring inline.

// ── Icons ────────────────────────────────────────────────────────────────
// Every key listed below MUST match a `case` in `Icon`'s `paths` map in
// `src/app/shared.tsx`. Adding an icon = adding a name here AND a path there.
export type IconName =
  | 'home'
  | 'plus'
  | 'folder'
  | 'doc'
  | 'db'
  | 'play'
  | 'check'
  | 'x'
  | 'chev'
  | 'chevd'
  | 'search'
  | 'bolt'
  | 'flag'
  | 'cog'
  | 'user'
  | 'site'
  | 'msg'
  | 'yaml'
  | 'arrow'
  | 'dot'
  | 'sun'
  | 'eye';

// ── Application (sample) shape ───────────────────────────────────────────
// Mirrors `SAMPLE_APPS` literal in `src/app/shared.tsx`. Status strings are
// the keys of `StatusBadge`'s map — anything outside this union falls back
// to the default badge styling.
export type AppStatus =
  | 'running'
  | 'done'
  | 'queued'
  | 'draft'
  | 'rendered'
  | 'incomplete'
  | 'failed'
  | 'gather'
  | 'review';

/**
 * Pipeline phase index (0-based). The design uses three primary phases
 * — gather (1), draft (2), render (3) — plus a "queued" zero-state (0).
 * null means the API returned an unrecognised phase string (e.g. 'unknown');
 * consumers treat null as "no known phase" and leave phase cards queued.
 */
export type AppPhase = 0 | 1 | 2 | 3 | null;

export interface SampleApp {
  slug: string;
  role: string;
  company: string;
  status: AppStatus;
  updated: string;
  phase: AppPhase;
  /** "14/14", "11/12", or "—" when not yet computed. */
  anchors: string;
  /** "pass", "1 flagged", "—". */
  factcheck: string;
  /** Filenames of rendered artefacts, e.g. ["resume.pdf", "cover.pdf"]. */
  renders: string[];
  url: string;
}

// ── Bullets ──────────────────────────────────────────────────────────────
export interface SampleBullet {
  id: string;
  /** Whether this bullet is currently anchored (kept verbatim across renders). */
  anchor: boolean;
  text: string;
}

// ── Theme + tweaks ───────────────────────────────────────────────────────
export type ThemeName = 'light' | 'dark' | 'paper';
export type Density = 'comfortable' | 'compact';

/**
 * Canonical defaults live in the EDITMODE block of the app entry — see
 * design/main.jsx. Keep this in sync with that literal.
 */
export interface TweakValues {
  theme: ThemeName;
  density: Density;
  showSlugColumn: boolean;
}

// ── Top-level navigation views ───────────────────────────────────────────
export type ViewName =
  | 'dashboard'
  | 'running'
  | 'review'
  | 'master'
  | 'anchors'
  | 'site'
  | 'feedback'
  | 'doctor'
  | 'config'
  | 'onboard';

// ── Status-badge mapping (used by `StatusBadge` in shared.tsx) ───────────
export type BadgeKind =
  | 'default'
  | 'success'
  | 'warn'
  | 'danger'
  | 'accent';
