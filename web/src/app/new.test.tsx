// new.test.tsx — accessibility + button type tests for NewApplicationModal
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { NewApplicationModal } from './new';

describe('NewApplicationModal', () => {
  it('renders with role=dialog and aria-modal', () => {
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={vi.fn()} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });

  it('pressing Escape calls onClose', () => {
    const onClose = vi.fn();
    render(<NewApplicationModal onClose={onClose} onLaunch={vi.fn()} />);
    const dialog = screen.getByRole('dialog');
    fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('cancel button has type="button" and clicking it does not submit the form', () => {
    const onClose = vi.fn();
    const onLaunch = vi.fn();
    render(<NewApplicationModal onClose={onClose} onLaunch={onLaunch} />);
    const cancelBtn = screen.getByRole('button', { name: /cancel/i });
    expect(cancelBtn).toHaveAttribute('type', 'button');
    fireEvent.click(cancelBtn);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onLaunch).not.toHaveBeenCalled();
  });

  it('review button has type="button" and advances to step 2', () => {
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={vi.fn()} />);
    const reviewBtn = screen.getByRole('button', { name: /review/i });
    expect(reviewBtn).toHaveAttribute('type', 'button');
    fireEvent.click(reviewBtn);
    expect(screen.getByText(/step 2 of 2/i)).toBeInTheDocument();
  });

  it('apply button on step 2 calls onLaunch with slug', () => {
    const onLaunch = vi.fn();
    render(<NewApplicationModal onClose={vi.fn()} onLaunch={onLaunch} />);
    // advance to step 2
    fireEvent.click(screen.getByRole('button', { name: /review/i }));
    const applyBtn = screen.getByRole('button', { name: /apply/i });
    expect(applyBtn).toHaveAttribute('type', 'submit');
    fireEvent.click(applyBtn);
    expect(onLaunch).toHaveBeenCalledTimes(1);
    expect(onLaunch.mock.calls[0][0]).toMatch(/linear-product-engineer/);
  });

  it('pressing Enter in the URL field does not trigger cancel', () => {
    const onClose = vi.fn();
    render(<NewApplicationModal onClose={onClose} onLaunch={vi.fn()} />);
    const urlInput = screen.getByPlaceholderText('https://...');
    fireEvent.keyDown(urlInput, { key: 'Enter', code: 'Enter' });
    expect(onClose).not.toHaveBeenCalled();
  });
});
