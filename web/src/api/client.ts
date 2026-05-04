// Minimal fetch wrapper for the jobsmith FastAPI backend.
//
// Centralised so slice 8 (SSE) can share the same base URL resolution and
// error envelope. Keep this file dependency-free — no React, no React Query.

const DEFAULT_BASE = 'http://localhost:8000';

function resolveBase(): string {
  // Vite injects `import.meta.env`; in the vitest environment with `jsdom` and
  // our setup file we still get the same shape. `VITE_*` vars are exposed.
  const fromEnv =
    typeof import.meta !== 'undefined' && import.meta.env
      ? (import.meta.env.VITE_API_BASE_URL as string | undefined)
      : undefined;
  return (fromEnv && fromEnv.length > 0 ? fromEnv : DEFAULT_BASE).replace(
    /\/+$/,
    '',
  );
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

/** Fetch JSON from the API. Throws ApiError on non-2xx. */
export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const url = apiUrl(path);
  const init: RequestInit = {
    method: 'GET',
    headers: { Accept: 'application/json' },
  };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  if (!resp.ok) {
    let body = '';
    try {
      body = await resp.text();
    } catch {
      // ignore — body is best-effort context
    }
    throw new ApiError(
      `GET ${url} → ${resp.status} ${resp.statusText}`,
      resp.status,
      url,
      body,
    );
  }
  return (await resp.json()) as T;
}

/**
 * POST JSON to the API and parse the JSON response. Throws ApiError on
 * non-2xx (the response body is preserved on the error so callers can
 * surface backend-supplied detail strings or richer 409 conflict payloads).
 */
export async function apiPost<TReq, TRes>(
  path: string,
  body: TReq,
  signal?: AbortSignal,
): Promise<TRes> {
  const url = apiUrl(path);
  const init: RequestInit = {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
  };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  if (!resp.ok) {
    let respBody = '';
    try {
      respBody = await resp.text();
    } catch {
      // ignore — best effort
    }
    throw new ApiError(
      `POST ${url} → ${resp.status} ${resp.statusText}`,
      resp.status,
      url,
      respBody,
    );
  }
  return (await resp.json()) as TRes;
}

/** PUT JSON to the API. Same error semantics as apiPost. */
export async function apiPut<TReq, TRes>(
  path: string,
  body: TReq,
  signal?: AbortSignal,
): Promise<TRes> {
  const url = apiUrl(path);
  const init: RequestInit = {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
  };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  if (!resp.ok) {
    let respBody = '';
    try {
      respBody = await resp.text();
    } catch {
      // best effort
    }
    throw new ApiError(
      `PUT ${url} → ${resp.status} ${resp.statusText}`,
      resp.status,
      url,
      respBody,
    );
  }
  return (await resp.json()) as TRes;
}

/** POST a multipart file upload. Used by /api/master/{section}/upload. */
export async function apiUploadFile<TRes>(
  path: string,
  file: File,
  signal?: AbortSignal,
): Promise<TRes> {
  const url = apiUrl(path);
  const form = new FormData();
  form.append('file', file);
  const init: RequestInit = { method: 'POST', body: form };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  if (!resp.ok) {
    let respBody = '';
    try {
      respBody = await resp.text();
    } catch {
      // best effort
    }
    throw new ApiError(
      `POST ${url} → ${resp.status} ${resp.statusText}`,
      resp.status,
      url,
      respBody,
    );
  }
  return (await resp.json()) as TRes;
}

/** Fetch raw text from the API (for /raw/{filename} endpoints). */
export async function apiGetText(
  path: string,
  signal?: AbortSignal,
): Promise<string> {
  const url = apiUrl(path);
  const init: RequestInit = { method: 'GET' };
  if (signal) init.signal = signal;
  const resp = await fetch(url, init);
  if (!resp.ok) {
    let body = '';
    try {
      body = await resp.text();
    } catch {
      // ignore
    }
    throw new ApiError(
      `GET ${url} → ${resp.status} ${resp.statusText}`,
      resp.status,
      url,
      body,
    );
  }
  return await resp.text();
}
