// BenchmarkEditor.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { BenchmarkEditor } from './BenchmarkEditor';

describe('BenchmarkEditor', () => {
  it('renders the textarea with current text', () => {
    render(<BenchmarkEditor text="# Hello" onChange={vi.fn()} />);
    const textarea = screen.getByLabelText('benchmark markdown source');
    expect(textarea).toHaveValue('# Hello');
  });

  it('renders the markdown preview', () => {
    render(<BenchmarkEditor text="# Title" onChange={vi.fn()} />);
    const preview = screen.getByLabelText('benchmark markdown preview');
    // react-markdown converts # to <h1>
    expect(preview.querySelector('h1')).toHaveTextContent('Title');
  });

  it('calls onChange when textarea is edited', () => {
    const onChange = vi.fn();
    render(<BenchmarkEditor text="" onChange={onChange} />);
    const textarea = screen.getByLabelText('benchmark markdown source');
    fireEvent.change(textarea, { target: { value: 'new content' } });
    expect(onChange).toHaveBeenCalledWith('new content');
  });

  it('renders empty textarea when text is empty', () => {
    render(<BenchmarkEditor text="" onChange={vi.fn()} />);
    const textarea = screen.getByLabelText('benchmark markdown source');
    expect(textarea).toHaveValue('');
  });
});
