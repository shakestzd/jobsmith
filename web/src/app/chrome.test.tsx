// chrome.test.tsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Sidebar } from './chrome';

describe('Sidebar', () => {
  function renderSidebar() {
    return render(
      <Sidebar view="dashboard" setView={vi.fn()} openNew={vi.fn()} />,
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
});
