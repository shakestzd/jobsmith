// Smoke test: +new modal step-2 apply button POSTs to /api/applications and
// invokes onLaunch with the slug returned by the API.

import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';

import { NewApplicationModal } from '../new';

interface FetchCall {
  url: string;
  init: RequestInit;
}

function stubFetch(responder: (call: FetchCall) => Response) {
  const calls: FetchCall[] = [];
  const original = global.fetch;
  global.fetch = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === 'string' ? input : (input as Request).url ?? String(input);
    const call: FetchCall = { url, init };
    calls.push(call);
    return responder(call);
  }) as typeof global.fetch;
  return {
    calls,
    restore: () => {
      global.fetch = original;
    },
  };
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...(init ?? {}),
  });
}

function renderModal(props: { onLaunch?: (slug: string) => void; onClose?: () => void } = {}) {
  const onLaunch = props.onLaunch ?? vi.fn();
  const onClose = props.onClose ?? vi.fn();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    React.createElement(
      QueryClientProvider,
      { client },
      React.createElement(NewApplicationModal, { onClose, onLaunch }),
    ),
  );
  return { onLaunch, onClose, ...utils };
}

describe('NewApplicationModal — apply mutation wiring', () => {
  let stub: ReturnType<typeof stubFetch>;
  afterEach(() => stub?.restore());

  it('POSTs /api/applications on step-2 apply and forwards the returned slug', async () => {
    stub = stubFetch(() =>
      jsonResponse({ slug: 'linear-product-engineer-2026-04', run_id: 'r-99', events_url: '/x' }, { status: 201 }),
    );
    const { onLaunch, getByText } = renderModal();

    // Step 1 → 2
    fireEvent.click(getByText('review →'));
    fireEvent.click(getByText('apply'));

    await waitFor(() => expect(onLaunch).toHaveBeenCalled());
    expect(onLaunch).toHaveBeenCalledWith('linear-product-engineer-2026-04');
    expect(stub.calls[0].init.method).toBe('POST');
    expect(stub.calls[0].url.endsWith('/api/applications')).toBe(true);
  });

  it('surfaces the server detail message inline on a 409', async () => {
    stub = stubFetch(() =>
      new Response(JSON.stringify({ detail: 'Application slug already exists.' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    const { onLaunch } = renderModal();

    fireEvent.click(screen.getByText('review →'));
    fireEvent.click(screen.getByText('apply'));

    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toMatch(/already exists/i),
    );
    expect(onLaunch).not.toHaveBeenCalled();
  });
});
