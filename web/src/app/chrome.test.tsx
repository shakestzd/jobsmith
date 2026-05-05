// chrome.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Sidebar } from './chrome';

describe('Sidebar', () => {
  function renderSidebar(openNew = vi.fn()) {
    return render(
      <Sidebar view="dashboard" setView={vi.fn()} openNew={openNew} />,
    );
  }

  it('does not render an "Outputs" section header', () => {
    renderSidebar();
    // The nav-section label "Outputs" was non-clickable and misleading — it must
    // not appear in the sidebar after feat-94f8bec1.
    expect(screen.queryByText('Outputs')).toBeNull();
  });

  it('still renders the Listings site and Feedback nav items', () => {
    renderSidebar();
    expect(screen.getByText('Listings site')).toBeInTheDocument();
    expect(screen.getByText('Feedback')).toBeInTheDocument();
  });

  it('new application button calls openNew, not a hardcoded route', () => {
    const openNew = vi.fn();
    renderSidebar(openNew);
    // The button text contains "new application" (with the ⌘N kbd hint alongside)
    const btn = screen.getByRole('button', { name: /new application/i });
    fireEvent.click(btn);
    expect(openNew).toHaveBeenCalledTimes(1);
  });
});
