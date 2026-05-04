// Minimal fetch wrapper for the jobsmith FastAPI backend.
//
// Centralised so events.ts (SSE) can share the same base URL resolution and
// error envelope. Keep this file dependency-free — no React, no React Query.
//
// Auth: all API routes require `Authorization: Bearer <token>`. The token is
// read from `import.meta.env.VITE_API_TOKEN` (set in .env.local). In tests
// the env var defaults to empty string which MSW intercepts anyway.

const DEFAULT_BASE = 'http://localhost:8000';

function resolveBase(): string {
  // Vite injects `import.meta.env`; in the vitest/jsdom environment we get
  // the same shape. `VITE_*` vars are exposed to the browser bundle.
  const fromEnv =
    typeof import.meta !== 'undefined' && import.meta.env
      ? (import.meta.env.VITE_API_BASE_URL as string | undefined)
      : undefined;
  return (fromEnv && fromEnv.length > 0 ? fromEnv : DEFAULT_BASE).replace(
    /\/+$/,
    '',
  );
}

function resolveToken(): string {
  const fromEnv =
    typeof import.meta !== 'undefined' && import.meta.env
      ? (import.meta.env.VITE_API_TOKEN as string | undefined)
      : undefined;
  return fromEnv ?? '';
}

export const API_BASE_URL: string = resolveBase();

export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  readonly body: string;

  constructor(message: string, status: number, url: string, body: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.url = url;
    this.body = body;
  }
}

/** Build a fully-qualified URL from a path that begins with `/api/...`. */
export function apiUrl(path: string): string {
  if (!path.startsWith('/')) {
    throw new Error(`apiUrl: path must start with "/", got: ${path}`);
  }
  return `${API_BASE_URL}${path}`;
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = resolveToken();
  const base: Record<string, string> = token
    ? { Authorization: `Bearer ${token}` }
    : {};
  return { ...base, ...extra };
}

async function throwOnError(resp: Response, url: string, method: string): Promise<void> {
  if (!resp.ok) {
    let body = '';
    try {
      body = await resp.text();
    } catch {
      // best effort
    }
    throw new ApiError(
      `${method} ${url} → ${resp.status} ${resp.statusText}`,
      resp.status,
      url,
      body,
    );
  }
}

/** Fetch JSON from the API. Throws ApiError on non-2xx. */
export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const url = apiUrl(path);
  const init: RequestInit = {
    method: 'GET',
    headers: authHeaders({ Accept: 'application/json' }),
  };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  await throwOnError(resp, url, 'GET');
  return (await resp.json()) as T;
}

/** Fetch raw text from the API. Throws ApiError on non-2xx. */
export async function apiGetText(
  path: string,
  signal?: AbortSignal,
): Promise<string> {
  const url = apiUrl(path);
  const init: RequestInit = {
    method: 'GET',
    headers: authHeaders(),
  };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  await throwOnError(resp, url, 'GET');
  return resp.text();
}

/** POST JSON to the API. Throws ApiError on non-2xx. */
export async function apiPost<TReq, TRes>(
  path: string,
  body: TReq,
  signal?: AbortSignal,
): Promise<TRes> {
  const url = apiUrl(path);
  const init: RequestInit = {
    method: 'POST',
    headers: authHeaders({
      'Content-Type': 'application/json',
      Accept: 'application/json',
    }),
    body: JSON.stringify(body),
  };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  await throwOnError(resp, url, 'POST');
  return (await resp.json()) as TRes;
}

/** PUT JSON to the API. Supports optional If-Match header for ETag concurrency. */
export async function apiPut<TReq, TRes>(
  path: string,
  body: TReq,
  opts?: { signal?: AbortSignal; ifMatch?: string },
): Promise<TRes> {
  const url = apiUrl(path);
  const extraHeaders: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  };
  if (opts?.ifMatch) {
    extraHeaders['If-Match'] = opts.ifMatch;
  }
  const init: RequestInit = {
    method: 'PUT',
    headers: authHeaders(extraHeaders),
    body: JSON.stringify(body),
  };
  if (opts?.signal) init.signal = opts.signal;
  const resp = await fetch(url, init);
  await throwOnError(resp, url, 'PUT');
  return (await resp.json()) as TRes;
}

/** POST a multipart file upload (for /api/master/{section}/upload). */
export async function apiPostMultipart<TRes>(
  path: string,
  file: File,
  signal?: AbortSignal,
): Promise<TRes> {
  const url = apiUrl(path);
  const form = new FormData();
  form.append('file', file);
  // Do NOT set Content-Type manually — browser sets it with the boundary.
  const init: RequestInit = {
    method: 'POST',
    headers: authHeaders({ Accept: 'application/json' }),
    body: form,
  };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  await throwOnError(resp, url, 'POST');
  return (await resp.json()) as TRes;
}
