// adapters.test.ts — round-trip tests for reverse adapters (feat-b28e9206).
//
// Each test verifies: reverse(forward(api)) ≅ api (structural equivalence).
// Also covers extras pass-through for author.

import { describe, it, expect } from 'vitest';
import {
  apiSkillsToForm,
  apiEducationToForm,
  apiAuthorToForm,
  formToApiSkills,
  formToApiEducation,
  formToApiAuthor,
} from './adapters';
import type { MasterSkillGroup, MasterEducationEntry, MasterAuthor } from '../../api/types';

// ── Skill round-trip ─────────────────────────────────────────────────────

const API_SKILLS: MasterSkillGroup[] = [
  { title: 'Languages', description: 'TypeScript', details: ['frontend', 'typed'] },
  { title: 'Tools',     description: 'Docker',     details: ['devops'] },
];

describe('formToApiSkills (reverse adapter)', () => {
  it('round-trips: apiSkillsToForm → formToApiSkills preserves api shape', () => {
    const form = apiSkillsToForm(API_SKILLS);
    const back = formToApiSkills(form);
    // title, description, details should match
    expect(back).toHaveLength(API_SKILLS.length);
    expect(back[0].title).toBe(API_SKILLS[0].title);
    expect(back[0].description).toBe(API_SKILLS[0].description);
    expect(back[0].details).toEqual(API_SKILLS[0].details);
    expect(back[1].title).toBe(API_SKILLS[1].title);
  });

  it('produces empty array from empty input', () => {
    expect(formToApiSkills([])).toEqual([]);
  });

  it('maps category → title, name → description, tags → details', () => {
    const result = formToApiSkills([
      { category: 'Frameworks', name: 'React', level: 5, tags: ['frontend', 'ui'] },
    ]);
    expect(result[0]).toMatchObject({
      title: 'Frameworks',
      description: 'React',
      details: ['frontend', 'ui'],
    });
  });
});

// ── Education round-trip ─────────────────────────────────────────────────

const API_EDUCATION: MasterEducationEntry[] = [
  {
    title: 'Massachusetts Institute of Technology',
    description: 'S.M., Technology and Policy',
    date: '2020',
    location: 'Cambridge, MA',
    details: ['Research in ML', 'Dean award'],
  },
];

describe('formToApiEducation (reverse adapter)', () => {
  it('round-trips: apiEducationToForm → formToApiEducation preserves api shape', () => {
    const form = apiEducationToForm(API_EDUCATION);
    const back = formToApiEducation(form);
    expect(back).toHaveLength(1);
    expect(back[0].title).toBe(API_EDUCATION[0].title);           // institution
    expect(back[0].description).toBe(API_EDUCATION[0].description); // degree
    expect(back[0].date).toBe(API_EDUCATION[0].date);             // year
    expect(back[0].location).toBe(API_EDUCATION[0].location);
    expect(back[0].details).toEqual(API_EDUCATION[0].details);
  });

  it('maps degree → description, institution → title, year → date', () => {
    const result = formToApiEducation([
      { degree: 'B.S. CS', institution: 'MIT', year: '2018', location: 'Cambridge', highlights: ['Dean list'] },
    ]);
    expect(result[0]).toMatchObject({
      title: 'MIT',
      description: 'B.S. CS',
      date: '2018',
      location: 'Cambridge',
      details: ['Dean list'],
    });
  });

  it('produces empty array from empty input', () => {
    expect(formToApiEducation([])).toEqual([]);
  });
});

// ── Author round-trip ────────────────────────────────────────────────────

const API_AUTHOR: MasterAuthor = {
  name: 'Jane Doe',
  email: 'jane@example.com',
  phone: '+1-555-0100',
  address: 'San Francisco, CA',
  position: 'Staff Engineer',
  contacts: [],
};

describe('formToApiAuthor (reverse adapter)', () => {
  it('round-trips: apiAuthorToForm → formToApiAuthor produces canonical {author:[...]} shape', () => {
    const form = apiAuthorToForm(API_AUTHOR);
    const back = formToApiAuthor(form);
    expect(back).toHaveProperty('author');
    const inner = (back.author as unknown[])[0] as Record<string, unknown>;
    expect(inner.name).toBe(API_AUTHOR.name);
    expect(inner.email).toBe(API_AUTHOR.email);
    expect(inner.phone).toBe(API_AUTHOR.phone);
    expect(inner.address).toBe(API_AUTHOR.address);
    expect(inner.position).toBe(API_AUTHOR.position);
  });

  it('preserves extra keys via __extras__ pass-through', () => {
    const extras = { quote: 'move fast', photo: 'photo.jpg' };
    const form = apiAuthorToForm(API_AUTHOR);
    const back = formToApiAuthor(form, extras);
    const inner = (back.author as unknown[])[0] as Record<string, unknown>;
    expect(inner.quote).toBe('move fast');
    expect(inner.photo).toBe('photo.jpg');
  });

  it('wraps result in {author: [...]} canonical shape', () => {
    const form = apiAuthorToForm(API_AUTHOR);
    const back = formToApiAuthor(form);
    expect(Array.isArray(back.author)).toBe(true);
    expect((back.author as unknown[]).length).toBe(1);
  });

  it('maps location → address, headline → position', () => {
    const form = apiAuthorToForm(API_AUTHOR);
    const back = formToApiAuthor(form);
    const inner = (back.author as unknown[])[0] as Record<string, unknown>;
    expect(inner.address).toBe(form.location);
    expect(inner.position).toBe(form.headline);
  });
});
