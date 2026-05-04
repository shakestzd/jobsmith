// EducationForm.tsx — per-degree rows: degree, institution, year, location, gpa (optional), highlights
import { type ChangeEvent } from 'react';
import type { EducationEntry } from './schemas';

interface EducationRowProps {
  entry: EducationEntry;
  onChange: (updated: EducationEntry) => void;
  onRemove: () => void;
}

function EducationRow({ entry, onChange, onRemove }: EducationRowProps) {
  const update = <K extends keyof EducationEntry>(key: K, value: EducationEntry[K]) => {
    onChange({ ...entry, [key]: value });
  };

  const updateHighlight = (index: number, value: string) => {
    const next = entry.highlights.map((h, i) => (i === index ? value : h));
    update('highlights', next);
  };

  const addHighlight = () => {
    update('highlights', [...entry.highlights, '']);
  };

  const removeHighlight = (index: number) => {
    update('highlights', entry.highlights.filter((_, i) => i !== index));
  };

  return (
    <div
      style={{
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius)',
        padding: '12px 14px',
        marginBottom: 10,
        background: 'var(--bg-sunk)',
      }}
    >
      {/* Row 1: degree + institution + year + location */}
      <div style={{ display: 'grid', gridTemplateColumns: '2fr 2fr 100px 1fr', gap: 8, marginBottom: 8 }}>
        <input
          type="text"
          value={entry.degree}
          placeholder="Degree"
          onChange={(e: ChangeEvent<HTMLInputElement>) => update('degree', e.target.value)}
          style={fieldStyle}
          aria-label="degree"
        />
        <input
          type="text"
          value={entry.institution}
          placeholder="Institution"
          onChange={(e: ChangeEvent<HTMLInputElement>) => update('institution', e.target.value)}
          style={fieldStyle}
          aria-label="institution"
        />
        <input
          type="text"
          value={entry.year}
          placeholder="Year"
          onChange={(e: ChangeEvent<HTMLInputElement>) => update('year', e.target.value)}
          style={fieldStyle}
          aria-label="year"
        />
        <input
          type="text"
          value={entry.location}
          placeholder="Location"
          onChange={(e: ChangeEvent<HTMLInputElement>) => update('location', e.target.value)}
          style={fieldStyle}
          aria-label="location"
        />
      </div>

      {/* Row 2: gpa (optional) */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
        <label style={labelStyle}>GPA (optional)</label>
        <input
          type="text"
          value={entry.gpa ?? ''}
          placeholder="e.g. 3.9"
          onChange={(e: ChangeEvent<HTMLInputElement>) =>
            update('gpa', e.target.value || undefined)
          }
          style={{ ...fieldStyle, width: 120 }}
          aria-label="gpa"
        />
        <div style={{ marginLeft: 'auto' }}>
          <button
            type="button"
            onClick={onRemove}
            aria-label="remove education entry"
            style={removeButtonStyle}
          >
            remove
          </button>
        </div>
      </div>

      {/* Highlights */}
      <div>
        <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
          Highlights
        </div>
        {entry.highlights.map((h, hi) => (
          <div key={hi} style={{ display: 'flex', gap: 6, marginBottom: 4 }}>
            <input
              type="text"
              value={h}
              placeholder="Highlight…"
              onChange={(e: ChangeEvent<HTMLInputElement>) => updateHighlight(hi, e.target.value)}
              style={{ ...fieldStyle, flex: 1 }}
              aria-label={`highlight ${hi + 1}`}
            />
            <button
              type="button"
              onClick={() => removeHighlight(hi)}
              aria-label={`remove highlight ${hi + 1}`}
              style={removeButtonStyle}
            >
              ×
            </button>
          </div>
        ))}
        <button
          type="button"
          className="btn ghost sm"
          onClick={addHighlight}
          style={{ marginTop: 2 }}
        >
          + add highlight
        </button>
      </div>
    </div>
  );
}

interface EducationFormProps {
  education: EducationEntry[];
  onChange: (updated: EducationEntry[]) => void;
}

export function EducationForm({ education, onChange }: EducationFormProps) {
  const updateEntry = (index: number, updated: EducationEntry) => {
    onChange(education.map((e, i) => (i === index ? updated : e)));
  };

  const removeEntry = (index: number) => {
    onChange(education.filter((_, i) => i !== index));
  };

  const addEntry = () => {
    onChange([
      ...education,
      { degree: '', institution: '', year: '', location: '', highlights: [] },
    ]);
  };

  return (
    <div className="card" style={{ padding: '12px 16px' }}>
      {education.map((entry, i) => (
        <EducationRow
          key={i}
          entry={entry}
          onChange={updated => updateEntry(i, updated)}
          onRemove={() => removeEntry(i)}
        />
      ))}
      <button
        type="button"
        className="btn ghost sm"
        onClick={addEntry}
        aria-label="add education entry"
      >
        + add education
      </button>
    </div>
  );
}

// ── Styles ───────────────────────────────────────────────────────────────

const fieldStyle: React.CSSProperties = {
  width: '100%',
  padding: '4px 8px',
  fontSize: 13,
  background: 'var(--bg)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius)',
  color: 'var(--fg)',
  boxSizing: 'border-box',
};

const labelStyle: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--fg-muted)',
  whiteSpace: 'nowrap',
};

const removeButtonStyle: React.CSSProperties = {
  background: 'none',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius)',
  cursor: 'pointer',
  padding: '2px 8px',
  fontSize: 12,
  color: 'var(--fg-muted)',
};
