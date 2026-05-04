// AuthorForm.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { AuthorForm } from './AuthorForm';
import { normaliseAuthor } from './schemas';
import type { Author } from './schemas';

const SAMPLE_AUTHOR: Author = {
  name: 'Jane Doe',
  email: 'jane@example.com',
  phone: '+1-555-0100',
  location: 'San Francisco, CA',
  headline: 'Senior Software Engineer',
  links: [
    { kind: 'github', url: 'https://github.com/janedoe' },
    { kind: 'linkedin', url: 'https://linkedin.com/in/janedoe' },
  ],
};

describe('AuthorForm', () => {
  it('renders all author fields', () => {
    render(<AuthorForm author={SAMPLE_AUTHOR} onChange={vi.fn()} />);
    expect(screen.getByDisplayValue('Jane Doe')).toBeInTheDocument();
    expect(screen.getByDisplayValue('jane@example.com')).toBeInTheDocument();
    expect(screen.getByDisplayValue('+1-555-0100')).toBeInTheDocument();
    expect(screen.getByDisplayValue('San Francisco, CA')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Senior Software Engineer')).toBeInTheDocument();
  });

  it('renders link rows with kind and url', () => {
    render(<AuthorForm author={SAMPLE_AUTHOR} onChange={vi.fn()} />);
    expect(screen.getByDisplayValue('https://github.com/janedoe')).toBeInTheDocument();
    expect(screen.getByDisplayValue('https://linkedin.com/in/janedoe')).toBeInTheDocument();
  });

  it('link kind dropdown has correct options', () => {
    render(<AuthorForm author={SAMPLE_AUTHOR} onChange={vi.fn()} />);
    const kindSelects = screen.getAllByRole('combobox');
    // each link has a kind select
    expect(kindSelects.length).toBeGreaterThanOrEqual(2);
    // check options are present
    const firstSelect = kindSelects[0];
    expect(firstSelect).toHaveValue('github');
  });

  it('calls onChange when name is edited', () => {
    const onChange = vi.fn();
    render(<AuthorForm author={SAMPLE_AUTHOR} onChange={onChange} />);
    const nameInput = screen.getByDisplayValue('Jane Doe');
    fireEvent.change(nameInput, { target: { value: 'Jane Smith' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: Author = onChange.mock.calls[0][0];
    expect(updated.name).toBe('Jane Smith');
  });

  it('adds a new link when add link button is clicked', () => {
    const onChange = vi.fn();
    render(<AuthorForm author={SAMPLE_AUTHOR} onChange={onChange} />);
    const addBtn = screen.getByRole('button', { name: /add link/i });
    fireEvent.click(addBtn);
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: Author = onChange.mock.calls[0][0];
    expect(updated.links).toHaveLength(3);
    expect(updated.links[2].kind).toBe('website');
    expect(updated.links[2].url).toBe('');
  });

  it('removes a link when remove button is clicked', () => {
    const onChange = vi.fn();
    render(<AuthorForm author={SAMPLE_AUTHOR} onChange={onChange} />);
    const removeBtns = screen.getAllByRole('button', { name: /remove link/i });
    fireEvent.click(removeBtns[0]); // remove github link
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: Author = onChange.mock.calls[0][0];
    expect(updated.links).toHaveLength(1);
    expect(updated.links[0].kind).toBe('linkedin');
  });
});

describe('normaliseAuthor', () => {
  it('handles bare-dict shape', () => {
    const raw = { name: 'Alice', email: 'alice@example.com' };
    const result = normaliseAuthor(raw);
    expect(result.name).toBe('Alice');
    expect(result.email).toBe('alice@example.com');
    expect(result.links).toEqual([]);
  });

  it('handles canonical wrapped shape { author: [{...}] }', () => {
    const raw = {
      author: [{ name: 'Bob', email: 'bob@example.com', phone: '555-1234' }],
    };
    const result = normaliseAuthor(raw);
    expect(result.name).toBe('Bob');
    expect(result.phone).toBe('555-1234');
  });

  it('handles null/undefined gracefully', () => {
    const result = normaliseAuthor(null);
    expect(result.name).toBe('');
  });
});
