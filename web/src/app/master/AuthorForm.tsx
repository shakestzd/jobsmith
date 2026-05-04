// AuthorForm.tsx — single-record author form
// Handles bare-dict AND {author: [{...}]} canonical shapes via normaliseAuthor.
import { type ChangeEvent } from 'react';
import type { Author, AuthorLink, LinkKind } from './schemas';

const LINK_KINDS: LinkKind[] = ['github', 'linkedin', 'website', 'other'];

interface LinkRowProps {
  link: AuthorLink;
  onChange: (updated: AuthorLink) => void;
  onRemove: () => void;
}

function LinkRow({ link, onChange, onRemove }: LinkRowProps) {
  return (
    <div style={{ display: 'flex', gap: 8, marginBottom: 6, alignItems: 'center' }}>
      <select
        value={link.kind}
        onChange={(e: ChangeEvent<HTMLSelectElement>) =>
          onChange({ ...link, kind: e.target.value as LinkKind })
        }
        style={{ ...fieldStyle, width: 120 }}
        aria-label="link kind"
      >
        {LINK_KINDS.map(k => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </select>
      <input
        type="text"
        value={link.url}
        placeholder="https://…"
        onChange={(e: ChangeEvent<HTMLInputElement>) =>
          onChange({ ...link, url: e.target.value })
        }
        style={{ ...fieldStyle, flex: 1 }}
        aria-label="link url"
      />
      <button
        type="button"
        onClick={onRemove}
        aria-label="remove link"
        style={removeButtonStyle}
      >
        ×
      </button>
    </div>
  );
}

interface AuthorFormProps {
  author: Author;
  onChange: (updated: Author) => void;
}

export function AuthorForm({ author, onChange }: AuthorFormProps) {
  const update = <K extends keyof Author>(key: K, value: Author[K]) => {
    onChange({ ...author, [key]: value });
  };

  const updateLink = (index: number, updated: AuthorLink) => {
    onChange({
      ...author,
      links: author.links.map((l, i) => (i === index ? updated : l)),
    });
  };

  const removeLink = (index: number) => {
    onChange({ ...author, links: author.links.filter((_, i) => i !== index) });
  };

  const addLink = () => {
    onChange({
      ...author,
      links: [...author.links, { kind: 'website', url: '' }],
    });
  };

  return (
    <div className="card" style={{ padding: '16px' }}>
      {/* Name + email + phone */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 160px', gap: 10, marginBottom: 10 }}>
        <div>
          <label style={labelStyle}>Name</label>
          <input
            type="text"
            value={author.name}
            placeholder="Full name"
            onChange={(e: ChangeEvent<HTMLInputElement>) => update('name', e.target.value)}
            style={fieldStyle}
            aria-label="name"
          />
        </div>
        <div>
          <label style={labelStyle}>Email</label>
          <input
            type="email"
            value={author.email}
            placeholder="you@example.com"
            onChange={(e: ChangeEvent<HTMLInputElement>) => update('email', e.target.value)}
            style={fieldStyle}
            aria-label="email"
          />
        </div>
        <div>
          <label style={labelStyle}>Phone</label>
          <input
            type="text"
            value={author.phone}
            placeholder="+1-555-0100"
            onChange={(e: ChangeEvent<HTMLInputElement>) => update('phone', e.target.value)}
            style={fieldStyle}
            aria-label="phone"
          />
        </div>
      </div>

      {/* Location + headline */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginBottom: 14 }}>
        <div>
          <label style={labelStyle}>Location</label>
          <input
            type="text"
            value={author.location}
            placeholder="City, State"
            onChange={(e: ChangeEvent<HTMLInputElement>) => update('location', e.target.value)}
            style={fieldStyle}
            aria-label="location"
          />
        </div>
        <div>
          <label style={labelStyle}>Headline</label>
          <input
            type="text"
            value={author.headline}
            placeholder="Senior Software Engineer"
            onChange={(e: ChangeEvent<HTMLInputElement>) => update('headline', e.target.value)}
            style={fieldStyle}
            aria-label="headline"
          />
        </div>
      </div>

      {/* Links */}
      <div>
        <div
          style={{
            fontSize: 11,
            fontFamily: 'var(--font-mono)',
            color: 'var(--fg-subtle)',
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            marginBottom: 8,
          }}
        >
          Links
        </div>
        {author.links.map((link, i) => (
          <LinkRow
            key={i}
            link={link}
            onChange={updated => updateLink(i, updated)}
            onRemove={() => removeLink(i)}
          />
        ))}
        <button
          type="button"
          className="btn ghost sm"
          onClick={addLink}
          aria-label="add link"
        >
          + add link
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
  marginTop: 3,
};

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 11,
  fontFamily: 'var(--font-mono)',
  color: 'var(--fg-subtle)',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  marginBottom: 2,
};

const removeButtonStyle: React.CSSProperties = {
  background: 'none',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius)',
  cursor: 'pointer',
  padding: '2px 6px',
  fontSize: 14,
  color: 'var(--fg-muted)',
};
