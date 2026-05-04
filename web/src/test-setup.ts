// Global vitest setup: wires msw + jest-dom matchers.
//
// Per-test handlers can be added with `server.use(...)` inside individual
// test files; the shared default handler set lives below.

import '@testing-library/jest-dom/vitest';
import { afterAll, afterEach, beforeAll } from 'vitest';
import { setupServer } from 'msw/node';

// Empty default — each test installs its own handlers via server.use(...)
// so we never silently match an unrelated request.
export const server = setupServer();

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
