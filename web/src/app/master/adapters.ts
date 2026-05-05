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
 */
export function apiEducationToForm(data: MasterEducationEntry[] | null | undefined): EducationEntry[] {
  if (!data) return [];
  return data.map((e): EducationEntry => ({
    degree: e.title ?? '',
    institution: e.location ?? '',
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
 * Adapt MasterAuthor API shape to the Author form shape.
 * Handles the name-as-object variant (`{ first, middle, last }`).
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

  return {
    name,
    email: data.email ?? '',
    phone: data.phone ?? '',
    location: data.address ?? '',
    headline: data.position ?? data.profession ?? '',
    links: [],
  };
}
