// SkillForm.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SkillForm } from './SkillForm';
import type { Skill } from './schemas';

const SAMPLE_SKILLS: Skill[] = [
  { category: 'Languages', name: 'TypeScript', level: 5, tags: ['frontend'] },
  { category: 'Languages', name: 'Python', level: 4, tags: [] },
  { category: 'Tools', name: 'Docker', level: 3, tags: ['devops', 'containers'] },
];

describe('SkillForm', () => {
  it('renders all skill rows', () => {
    render(<SkillForm skills={SAMPLE_SKILLS} onChange={vi.fn()} />);
    expect(screen.getByDisplayValue('TypeScript')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Python')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Docker')).toBeInTheDocument();
  });

  it('renders level selects with values 1-5', () => {
    render(<SkillForm skills={SAMPLE_SKILLS} onChange={vi.fn()} />);
    const selects = screen.getAllByRole('spinbutton');
    // first skill has level 5
    expect(selects[0]).toHaveValue(5);
    // second skill has level 4
    expect(selects[1]).toHaveValue(4);
  });

  it('level input clamps to 1-5 range attributes', () => {
    render(<SkillForm skills={SAMPLE_SKILLS} onChange={vi.fn()} />);
    const levelInputs = screen.getAllByRole('spinbutton');
    expect(levelInputs[0]).toHaveAttribute('min', '1');
    expect(levelInputs[0]).toHaveAttribute('max', '5');
  });

  it('calls onChange when a skill name is edited', () => {
    const onChange = vi.fn();
    render(<SkillForm skills={SAMPLE_SKILLS} onChange={onChange} />);
    const nameInput = screen.getByDisplayValue('TypeScript');
    fireEvent.change(nameInput, { target: { value: 'TypeScript 5' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: Skill[] = onChange.mock.calls[0][0];
    expect(updated[0].name).toBe('TypeScript 5');
  });

  it('adds a new empty skill row when add button is clicked', () => {
    const onChange = vi.fn();
    render(<SkillForm skills={SAMPLE_SKILLS} onChange={onChange} />);
    const addBtn = screen.getByRole('button', { name: /add skill/i });
    fireEvent.click(addBtn);
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: Skill[] = onChange.mock.calls[0][0];
    expect(updated).toHaveLength(4);
    expect(updated[3].name).toBe('');
  });

  it('removes a skill row when remove button is clicked', () => {
    const onChange = vi.fn();
    render(<SkillForm skills={SAMPLE_SKILLS} onChange={onChange} />);
    // aria-label "remove skill" is unique to the row delete button
    // (tag deletes use "remove tag <tag>")
    const removeBtns = screen.getAllByLabelText('remove skill');
    fireEvent.click(removeBtns[1]); // remove Python (second row)
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: Skill[] = onChange.mock.calls[0][0];
    expect(updated).toHaveLength(2);
    expect(updated.find(s => s.name === 'Python')).toBeUndefined();
  });

  it('renders category select with correct value', () => {
    render(<SkillForm skills={SAMPLE_SKILLS} onChange={vi.fn()} />);
    // first skill's category select
    const categoryInputs = screen.getAllByDisplayValue('Languages');
    expect(categoryInputs.length).toBeGreaterThanOrEqual(1);
  });
});
