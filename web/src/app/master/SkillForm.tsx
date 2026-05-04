// SkillForm.tsx — per-skill rows: category, name, level (1-5), tags
import { useState } from 'react';
import type { Skill } from './schemas';

// Common skill categories for the select dropdown
const CATEGORIES = [
  'Languages',
  'Frameworks',
  'Tools',
  'Platforms',
  'Databases',
  'Practices',
  'Other',
];

interface SkillRowProps {
  skill: Skill;
  index: number;
  onChange: (updated: Skill) => void;
  onRemove: () => void;
}

function SkillRow({ skill, index: _index, onChange, onRemove }: SkillRowProps) {
  const [tagInput, setTagInput] = useState('');

  const update = <K extends keyof Skill>(key: K, value: Skill[K]) => {
    onChange({ ...skill, [key]: value });
  };

  const addTag = () => {
    const trimmed = tagInput.trim();
    if (trimmed && !skill.tags.includes(trimmed)) {
      update('tags', [...skill.tags, trimmed]);
      setTagInput('');
    }
  };

  const removeTag = (tag: string) => {
    update('tags', skill.tags.filter(t => t !== tag));
  };

  const handleTagKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      addTag();
    }
  };

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '180px 1fr 80px 1fr auto',
        gap: 8,
        alignItems: 'start',
        padding: '8px 0',
        borderBottom: '1px solid var(--border)',
      }}
    >
      {/* Category */}
      <input
        list="skill-categories"
        value={skill.category}
        placeholder="Category"
        onChange={e => update('category', e.target.value)}
        style={fieldStyle}
      />
      <datalist id="skill-categories">
        {CATEGORIES.map(c => <option key={c} value={c} />)}
      </datalist>

      {/* Name */}
      <input
        type="text"
        value={skill.name}
        placeholder="Skill name"
        onChange={e => update('name', e.target.value)}
        style={fieldStyle}
      />

      {/* Level 1-5 */}
      <input
        type="number"
        min={1}
        max={5}
        value={skill.level}
        onChange={e => {
          const v = Math.min(5, Math.max(1, parseInt(e.target.value, 10) || 1));
          update('level', v);
        }}
        style={fieldStyle}
      />

      {/* Tags chip input */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
        {skill.tags.map(tag => (
          <span
            key={tag}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 3,
              padding: '2px 6px',
              borderRadius: 10,
              background: 'var(--bg-sunk)',
              fontSize: 11,
              border: '1px solid var(--border)',
            }}
          >
            {tag}
            <button
              type="button"
              onClick={() => removeTag(tag)}
              aria-label={`remove tag ${tag}`}
              style={chipRemoveStyle}
            >
              ×
            </button>
          </span>
        ))}
        <input
          type="text"
          value={tagInput}
          onChange={e => setTagInput(e.target.value)}
          onKeyDown={handleTagKeyDown}
          onBlur={addTag}
          placeholder="add tag…"
          style={{ ...fieldStyle, width: 80, minWidth: 60 }}
        />
      </div>

      {/* Remove row */}
      <button
        type="button"
        onClick={onRemove}
        aria-label="remove skill"
        style={removeButtonStyle}
      >
        ×
      </button>
    </div>
  );
}

interface SkillFormProps {
  skills: Skill[];
  onChange: (updated: Skill[]) => void;
}

export function SkillForm({ skills, onChange }: SkillFormProps) {
  const updateSkill = (index: number, updated: Skill) => {
    const next = skills.map((s, i) => (i === index ? updated : s));
    onChange(next);
  };

  const removeSkill = (index: number) => {
    onChange(skills.filter((_, i) => i !== index));
  };

  const addSkill = () => {
    onChange([
      ...skills,
      { category: '', name: '', level: 3, tags: [] },
    ]);
  };

  return (
    <div className="card" style={{ padding: '12px 16px' }}>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '180px 1fr 80px 1fr auto',
          gap: 8,
          padding: '0 0 6px',
          borderBottom: '2px solid var(--border)',
          fontSize: 11,
          fontFamily: 'var(--font-mono)',
          color: 'var(--fg-subtle)',
          textTransform: 'uppercase',
          letterSpacing: '0.06em',
        }}
      >
        <span>Category</span>
        <span>Skill</span>
        <span>Level</span>
        <span>Tags</span>
        <span />
      </div>

      {skills.map((skill, i) => (
        <SkillRow
          key={i}
          skill={skill}
          index={i}
          onChange={updated => updateSkill(i, updated)}
          onRemove={() => removeSkill(i)}
        />
      ))}

      <div style={{ paddingTop: 10 }}>
        <button
          type="button"
          className="btn ghost sm"
          onClick={addSkill}
          aria-label="add skill"
        >
          + add skill
        </button>
      </div>
    </div>
  );
}

// ── Styles ───────────────────────────────────────────────────────────────

const fieldStyle: React.CSSProperties = {
  width: '100%',
  padding: '4px 8px',
  fontSize: 13,
  background: 'var(--bg-sunk)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius)',
  color: 'var(--fg)',
  boxSizing: 'border-box',
};

const chipRemoveStyle: React.CSSProperties = {
  background: 'none',
  border: 'none',
  cursor: 'pointer',
  padding: 0,
  lineHeight: 1,
  fontSize: 13,
  color: 'var(--fg-muted)',
};

const removeButtonStyle: React.CSSProperties = {
  background: 'none',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius)',
  cursor: 'pointer',
  padding: '2px 6px',
  fontSize: 14,
  color: 'var(--fg-muted)',
  alignSelf: 'center',
};
