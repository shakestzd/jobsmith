// schemas.ts — Zod schemas mirroring the Pydantic master models
// Used for client-side validation in SkillForm, EducationForm, AuthorForm.

import { z } from 'zod';

// ── Skill ────────────────────────────────────────────────────────────────

export const SkillSchema = z.object({
  category: z.string().min(1, 'Category required'),
  name: z.string().min(1, 'Skill name required'),
  level: z.number().int().min(1).max(5),
  tags: z.array(z.string()).default([]),
});

export type Skill = z.infer<typeof SkillSchema>;

export const SkillsSchema = z.array(SkillSchema);
export type Skills = z.infer<typeof SkillsSchema>;

// ── Education ────────────────────────────────────────────────────────────

export const EducationEntrySchema = z.object({
  degree: z.string().min(1, 'Degree required'),
  institution: z.string().min(1, 'Institution required'),
  year: z.string().min(1, 'Year required'),
  location: z.string().default(''),
  gpa: z.string().optional(),
  highlights: z.array(z.string()).default([]),
});

export type EducationEntry = z.infer<typeof EducationEntrySchema>;

export const EducationSchema = z.array(EducationEntrySchema);
export type Education = z.infer<typeof EducationSchema>;

// ── Author ───────────────────────────────────────────────────────────────

export const LinkKindSchema = z.enum(['github', 'linkedin', 'website', 'other']);
export type LinkKind = z.infer<typeof LinkKindSchema>;

export const AuthorLinkSchema = z.object({
  kind: LinkKindSchema,
  url: z.string().url('Must be a valid URL'),
});

export type AuthorLink = z.infer<typeof AuthorLinkSchema>;

export const AuthorSchema = z.object({
  name: z.string().min(1, 'Name required'),
  email: z.string().email('Must be a valid email'),
  phone: z.string().default(''),
  links: z.array(AuthorLinkSchema).default([]),
  location: z.string().default(''),
  headline: z.string().default(''),
});

export type Author = z.infer<typeof AuthorSchema>;

// ── Normaliser: bare-dict OR {author: [{...}]} → Author ─────────────────

const EMPTY_AUTHOR: Author = {
  name: '',
  email: '',
  phone: '',
  location: '',
  headline: '',
  links: [],
};

function safeParse(raw: unknown): Author {
  // Permissive parse: defaults missing required fields rather than throwing.
  // Strict validation happens server-side on save.
  if (raw == null || typeof raw !== 'object' || Array.isArray(raw)) return { ...EMPTY_AUTHOR };
  const obj = raw as Record<string, unknown>;
  return {
    name: typeof obj.name === 'string' ? obj.name : '',
    email: typeof obj.email === 'string' ? obj.email : '',
    phone: typeof obj.phone === 'string' ? obj.phone : '',
    location: typeof obj.location === 'string' ? obj.location : '',
    headline: typeof obj.headline === 'string' ? obj.headline : '',
    links: Array.isArray(obj.links)
      ? obj.links.flatMap((l) => {
          const r = AuthorLinkSchema.safeParse(l);
          return r.success ? [r.data] : [];
        })
      : [],
  };
}

export function normaliseAuthor(raw: unknown): Author {
  if (raw == null) return { ...EMPTY_AUTHOR };

  // canonical shape: { author: [{ ... }] }
  if (
    typeof raw === 'object' &&
    !Array.isArray(raw) &&
    'author' in (raw as object)
  ) {
    const wrapped = (raw as { author: unknown }).author;
    const first = Array.isArray(wrapped) ? wrapped[0] : wrapped;
    return safeParse(first);
  }

  // bare-dict shape: { name: ..., email: ..., ... }
  return safeParse(raw);
}
