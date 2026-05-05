// adapters.ts — forward-only adapters from API shapes to form shapes.
//
// These are intentionally one-way (API → form). No reverse mapping is
// attempted — saves are lossy until feat-6999e552 ships the ETag round-trip.
//
// Exports:
//   apiWorkToRoles       MasterWorkRole[] (pass-through; already the right shape)
//   apiSkillsToForm      MasterSkillGroup[] → Skill[]
//   apiEducationToForm   MasterEducationEntry[] → EducationEntry[]
//   apiAuthorToForm      MasterAuthor | null → Author

import type { MasterWorkRole, MasterSkillGroup, MasterEducationEntry, MasterAuthor } from '../../api/types';
import type { Skill, EducationEntry, Author } from './schemas';

// ── Work ─────────────────────────────────────────────────────────────────

/**
 * Work roles are already MasterWorkRole[] from the API — pass through.
 * Returns [] when data is undefined/null.
 */
export function apiWorkToRoles(data: MasterWorkRole[] | undefined | null): MasterWorkRole[] {
  return data ?? [];
}

// ── Skills ───────────────────────────────────────────────────────────────

/**
 * Adapt MasterSkillGroup[] to the flat Skill[] shape the SkillForm expects.
 * Each group item produces one Skill row; missing fields are defaulted.
 */
export function apiSkillsToForm(data: MasterSkillGroup[] | null | undefined): Skill[] {
  if (!data) return [];
  return data.map((g): Skill => ({
    category: g.title ?? '',
    name: g.description ?? '',
    level: 3,
    tags: Array.isArray(g.details) ? g.details : [],
  }));
}

// ── Education ────────────────────────────────────────────────────────────

/**
 * Adapt MasterEducationEntry[] to EducationEntry[] for EducationForm.
 *
 * API field mapping (per src/jobsmith/api/types.ts):
 *   title       → institution (e.g. "Massachusetts Institute of Technology")
 *   description → degree      (e.g. "S.M., Technology and Policy")
 *   location, date pass through.
 */
export function apiEducationToForm(data: MasterEducationEntry[] | null | undefined): EducationEntry[] {
  if (!data) return [];
  return data.map((e): EducationEntry => ({
    degree: e.description ?? '',
    institution: e.title ?? '',
    year: e.date ?? '',
    location: e.location ?? '',
    gpa: undefined,
    highlights: Array.isArray(e.details) ? e.details : [],
  }));
}

// ── Author ───────────────────────────────────────────────────────────────

const EMPTY_AUTHOR: Author = {
  name: '',
  email: '',
  phone: '',
  location: '',
  headline: '',
  links: [],
};

/**
 * Best-effort classify a fontawesome-style `icon` string → link kind.
 * Brand contacts ("fa brands github", "fa brands linkedin") map to known kinds;
 * everything else falls through to "other".
 */
function brandFromIcon(icon: string | undefined): 'github' | 'linkedin' | 'website' | 'other' {
  const lower = (icon ?? '').toLowerCase();
  if (lower.includes('github')) return 'github';
  if (lower.includes('linkedin')) return 'linkedin';
  if (lower.includes('globe') || lower.includes('home') || lower.includes('website')) return 'website';
  return 'other';
}

/**
 * Adapt MasterAuthor API shape to the Author form shape.
 *
 * The API often emits email/phone/location/links inside `contacts[]` keyed
 * by fontawesome icon name ("fa envelope", "fa phone", "fa location-crosshairs",
 * "fa brands github") rather than top-level fields. Fall back to contacts
 * when top-level keys are empty so users see real values, not placeholders.
 */
export function apiAuthorToForm(data: MasterAuthor | null | undefined): Author {
  if (!data) return { ...EMPTY_AUTHOR };

  // name may be a string or { first, middle, last } object.
  let name = '';
  if (typeof data.name === 'string') {
    name = data.name;
  } else if (data.name && typeof data.name === 'object') {
    const n = data.name as Record<string, string>;
    name = [n['first'], n['middle'], n['last']].filter(Boolean).join(' ');
  } else if (data.firstname || data.lastname) {
    name = [data.firstname, data.lastname].filter(Boolean).join(' ');
  }

  const contacts = data.contacts ?? [];
  const findContact = (needle: string) =>
    contacts.find((c) => (c.icon ?? '').toLowerCase().includes(needle));
  const emailContact = findContact('envelope');
  const phoneContact = findContact('phone');
  const locationContact = findContact('location');

  // Brand contacts → typed Author.links rows.
  const links = contacts
    .filter((c) => Boolean(c.url) && (c.icon ?? '').toLowerCase().includes('brands'))
    .map((c) => ({ kind: brandFromIcon(c.icon), url: c.url as string }));

  return {
    name,
    email: data.email || emailContact?.text || '',
    phone: data.phone || phoneContact?.text || '',
    location: data.address || locationContact?.text || '',
    headline: data.position ?? data.profession ?? '',
    links,
  };
}
