# Frontend Feature DOD (Definition of Done)

This document defines the **minimum bar** for a frontend feature to be considered "done" in this repo. It exists because we shipped multiple features whose UI was hardcoded fixture data — passing tests, looking real, but never wired to the API. The DOD makes that class of regression impossible to merge.

## Scope

Applies to any feature that adds, changes, or wires a piece of UI under `web/src/` against an API endpoint. Does **not** apply to:

- Pure styling / layout-only changes with no data dependency
- Backend-only changes that don't touch `web/src/`

## The three artifacts

A frontend feature is not done until **all three** are present in the PR:

### 1. MSW vitest asserting an API-only fixture value reaches the DOM

A vitest test file (`*.test.tsx` next to the component) that:

- Mocks `../api/client` with `vi.mock(...)` so the component's API calls return a **synthetic, recognizable value** that does not appear anywhere else in the codebase (e.g. `"FROM_API_FIXTURE_x9q2"` for a string field, `"42424242"` for a number).
- Renders the component and waits for the API-derived value to appear in the DOM via `screen.getByText(...)` or `container.textContent`.
- For features that mutate (POST/PUT/DELETE), additionally asserts the mock mutation handler was called with the expected payload via `expect(apiPost).toHaveBeenCalledWith(...)`.

If the component is silently re-fixtured (a developer reverts to hardcoded data), this test fails because the fixture sentinel string never makes it to the DOM.

**Naming convention:** the synthetic value should be obvious-on-grep. Use a stable prefix like `FROM_API_` or a numeric pattern like `42424242` so it can never be confused with real data.

**Reference implementations:** `web/src/app/dashboard.test.tsx`, `web/src/app/feedback.test.tsx` — both follow the `vi.mock('../api/client', ...)` pattern.

**Skeleton:**

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MyComponent } from './my-component';

vi.mock('../api/client', () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  postApplication: vi.fn(),
  buildEventsUrl: vi.fn(),
  JobsmithApiError: class JobsmithApiError extends Error { status = 500; },
}));

import { apiGet, apiPost } from '../api/client';

describe('MyComponent', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders API-derived field (not a fixture)', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({
      title: 'FROM_API_FIXTURE_x9q2',
    });
    render(<MyComponent />);
    await waitFor(() => {
      expect(screen.getByText('FROM_API_FIXTURE_x9q2')).toBeInTheDocument();
    });
  });

  it('mutation handler fires on submit', async () => {
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true });
    render(<MyComponent />);
    // ...interact, then:
    expect(apiPost).toHaveBeenCalledWith(
      '/api/whatever',
      expect.objectContaining({ /* expected payload */ }),
    );
  });
});
```

### 2. Manual smoke evidence — DOM excerpt pasted into the feature step

Run the dev server (`npm run dev` from `web/`), exercise the feature against the live backend, and paste a DOM excerpt (or screenshot of the network panel + element inspector) into the `htmlgraph feature show {id}` step body proving the API response value reached the DOM.

What to capture:

- The actual API response body (curl or DevTools Network tab)
- The DOM element rendering that response value (DevTools Elements tab, or `outerHTML` from console)
- A timestamp showing both were observed in the same window

This catches the case where the test passes (because the mock works) but the production wiring is wrong (because the real component subscribes to a different field, has a stale selector, or reads from local state that never gets updated).

### 3. Anti-regression grep proving named fixture strings are absent

For features that **replace** existing fixture content, add a grep-style regression test that fails if the named fixture strings reappear in the rendered DOM. Put it in the same vitest file:

```tsx
it('does not render any of the legacy fixture strings', async () => {
  (apiGet as ReturnType<typeof vi.fn>).mockResolvedValue({ /* minimal API response */ });
  const { container } = render(<MyComponent />);
  await waitFor(() => { /* component is loaded */ });
  const text = container.textContent ?? '';
  // Each of these is a literal that was hardcoded in the pre-fix version.
  // If any reappear, a developer is silently reintroducing fixtures.
  expect(text).not.toMatch(/14:02:01/);            // fake event timestamp
  expect(text).not.toMatch(/Recurly Engineering/); // fake cover-draft text
  expect(text).not.toMatch(/11m → 2m20s/);         // fake metric
  expect(text).not.toMatch(/\$140k\/yr/);          // fake metric
});
```

**Source the strings from the GitHub issue or the original mock.** They should be the *exact* literals a developer would re-type if reverting to fixtures. If the test description is too generic to point at a regression, it's not specific enough.

## When all three are missing

A PR may merge with **fewer** than three artifacts only if:

- The feature is an internal refactor with no new data wiring (no API call added or changed), AND
- The reviewer explicitly waives the manual-smoke artifact in their review

In all other cases, a frontend PR with mock-data-shaped output and no anti-regression grep is considered **not done** and should be sent back.

## Rationale

The same failure mode shipped in PRs #29 and #46:

- A component's "wired-up" claim was based on a passing unit test that mocked the API to return `undefined` and asserted the component "didn't crash."
- Manual testing was done against fixture mode (vite dev server with no `VITE_JOBSMITH_API_URL` set), so the fixture path was the only thing exercised.
- Subsequent reverts to hardcoded data slipped past CI because no test asserted "the API value, specifically, is what the user sees."

The DOD removes all three escape hatches.

## Related

- `htmlgraph feature complete <id>` — only run after all three artifacts exist
- `web/src/app/dashboard.test.tsx`, `web/src/app/feedback.test.tsx` — reference implementations of artifacts 1 and 3
- GitHub issues #51, #52, #53 — exemplars of the regressions this DOD prevents
