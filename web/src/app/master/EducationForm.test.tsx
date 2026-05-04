// EducationForm.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { EducationForm } from './EducationForm';
import type { EducationEntry } from './schemas';

const SAMPLE_EDUCATION: EducationEntry[] = [
  {
    degree: 'BSc Computer Science',
    institution: 'MIT',
    year: '2015',
    location: 'Cambridge, MA',
    gpa: '3.9',
    highlights: ['Dean\'s List', 'Thesis award'],
  },
  {
    degree: 'MSc Machine Learning',
    institution: 'Stanford',
    year: '2017',
    location: 'Palo Alto, CA',
    highlights: [],
  },
];

describe('EducationForm', () => {
  it('renders all education rows', () => {
    render(<EducationForm education={SAMPLE_EDUCATION} onChange={vi.fn()} />);
    expect(screen.getByDisplayValue('BSc Computer Science')).toBeInTheDocument();
    expect(screen.getByDisplayValue('MSc Machine Learning')).toBeInTheDocument();
  });

  it('renders institution, year, and location fields', () => {
    render(<EducationForm education={SAMPLE_EDUCATION} onChange={vi.fn()} />);
    expect(screen.getByDisplayValue('MIT')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Stanford')).toBeInTheDocument();
    expect(screen.getByDisplayValue('2015')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Cambridge, MA')).toBeInTheDocument();
  });

  it('renders optional gpa when present', () => {
    render(<EducationForm education={SAMPLE_EDUCATION} onChange={vi.fn()} />);
    expect(screen.getByDisplayValue('3.9')).toBeInTheDocument();
  });

  it('gpa field is optional — second entry has no gpa without error', () => {
    const noGpa: EducationEntry[] = [
      { degree: 'BSc CS', institution: 'MIT', year: '2015', location: '', highlights: [] },
    ];
    // should render without throwing
    const { container } = render(<EducationForm education={noGpa} onChange={vi.fn()} />);
    expect(container).toBeTruthy();
  });

  it('calls onChange when degree is edited', () => {
    const onChange = vi.fn();
    render(<EducationForm education={SAMPLE_EDUCATION} onChange={onChange} />);
    const degreeInput = screen.getByDisplayValue('BSc Computer Science');
    fireEvent.change(degreeInput, { target: { value: 'BSc Computer Science (Hons)' } });
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: EducationEntry[] = onChange.mock.calls[0][0];
    expect(updated[0].degree).toBe('BSc Computer Science (Hons)');
  });

  it('adds a new entry when add button is clicked', () => {
    const onChange = vi.fn();
    render(<EducationForm education={SAMPLE_EDUCATION} onChange={onChange} />);
    const addBtn = screen.getByRole('button', { name: /add education/i });
    fireEvent.click(addBtn);
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: EducationEntry[] = onChange.mock.calls[0][0];
    expect(updated).toHaveLength(3);
    expect(updated[2].degree).toBe('');
  });

  it('removes an entry when remove button is clicked', () => {
    const onChange = vi.fn();
    render(<EducationForm education={SAMPLE_EDUCATION} onChange={onChange} />);
    const removeBtns = screen.getAllByRole('button', { name: /remove/i });
    fireEvent.click(removeBtns[0]); // remove first
    expect(onChange).toHaveBeenCalledTimes(1);
    const updated: EducationEntry[] = onChange.mock.calls[0][0];
    expect(updated).toHaveLength(1);
    expect(updated[0].degree).toBe('MSc Machine Learning');
  });
});
