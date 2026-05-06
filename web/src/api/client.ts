// client.ts — Thin fetch-based HTTP client for the jobsmith API.
//
// Exports:
//   JobsmithApiError  — typed error class; callers check `.status`
//   apiGet            — authenticated GET, returns parsed JSON
//   apiGetWithMeta    — authenticated GET, returns { data, etag, status }
//   apiPost           — authenticated POST with JSON body, returns parsed JSON
//   apiPut            — authenticated PUT with JSON body + optional If-Match header
//   apiDelete         — authenticated DELETE with optional JSON body
//   postApplication   — POST /api/applications → { slug, run_id }
//   buildEventsUrl    — construct the SSE URL for a slug

const BASE_URL = import.meta.env.VITE_JOBSMITH_API_URL ?? 'http://localhost:8000';
const TOKEN = import.meta.env.VITE_JOBSMITH_API_TOKEN ?? '';

// ── Error class ──────────────────────────────────────────────────────────

export class JobsmithApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'JobsmithApiError';
    this.status = status;
  }
}

// ── Internal helpers ─────────────────────────────────────────────────────

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (TOKEN) {
    headers['Authorization'] = `Bearer ${TOKEN}`;
  }
  return headers;
}

export function formatDetail(rawDetail: unknown, fallback: string): string {
  if (rawDetail == null) return fallback;
  if (typeof rawDetail === 'string') return rawDetail;
  if (typeof rawDetail === 'object') {
    // Structured 404 from S3 (feat-eb6c99cb): {error, section?, suggestion}
    const obj = rawDetail as Record<string, unknown>;
    const error = typeof obj.error === 'string' ? obj.error : null;
    const suggestion = typeof obj.suggestion === 'string' ? obj.suggestion : null;
    if (error && suggestion) return `${error} — ${suggestion}`;
    if (error) return error;
    if (suggestion) return suggestion;
  }
  return fallback;
}

async function throwIfError(res: Response): Promise<void> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = formatDetail(body?.detail, detail);
    } catch {
      // ignore json parse failure
    }
    throw new JobsmithApiError(detail, res.status);
  }
}

// ── Public helpers ───────────────────────────────────────────────────────

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: authHeaders(),
  });
  await throwIfError(res);
  return res.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  await throwIfError(res);
  return res.json() as Promise<T>;
}

export async function apiPut<T>(
  path: string,
  body: unknown,
  options: { ifMatch?: string; headers?: Record<string, string> } = {},
): Promise<T> {
  const headers: Record<string, string> = { ...authHeaders(), ...options.headers };
  if (options.ifMatch !== undefined) {
    // Strip surrounding double-quotes from the ETag value before sending —
    // ETag headers arrive quoted but the backend's strip is single-pass.
    headers['If-Match'] = options.ifMatch.replace(/^"|"$/g, '');
  }
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'PUT',
    headers,
    body: JSON.stringify(body),
  });
  await throwIfError(res);
  return res.json() as Promise<T>;
}

export async function apiGetWithMeta<T>(
  path: string,
): Promise<{ data: T; etag: string | null; status: number }> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: authHeaders(),
  });
  await throwIfError(res);
  const data = (await res.json()) as T;
  const etag = res.headers.get('etag');
  return { data, etag, status: res.status };
}

export async function apiDelete<T>(path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { ...authHeaders() };
  const init: RequestInit = { method: 'DELETE', headers };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    headers['Content-Type'] = 'application/json';
  } else {
    // No body — remove Content-Type to avoid misleading empty-body requests.
    delete headers['Content-Type'];
  }
  const res = await fetch(`${BASE_URL}${path}`, init);
  await throwIfError(res);
  return res.json() as Promise<T>;
}

// ── Applications API ─────────────────────────────────────────────────────

export interface ApplicationCreated {
  slug: string;
  run_id: string;
}

/**
 * POST /api/applications — launch an apply run.
 * Returns { slug, run_id } on 201.
 *
 * When `force` is true, the server invokes `jobsmith apply --force`, which
 * restarts the pipeline from phase 1 even if prior artifacts exist. Required
 * to re-run any application whose `.apply-state/` is already complete.
 */
export function postApplication(
  url: string,
  slug: string,
  options: { force?: boolean; jdText?: string } = {},
): Promise<ApplicationCreated> {
  // bug-1c800e09: when paste-text mode is active, send jd_text so the backend
  // skips URL fetching (required for JS-rendered ATS portals like Eightfold).
  const body: Record<string, unknown> = {
    url,
    slug,
    force: options.force === true,
  };
  if (options.jdText && options.jdText.trim() !== '') {
    body.jd_text = options.jdText;
  }
  return apiPost<ApplicationCreated>('/api/applications', body);
}

/**
 * Build the SSE URL for a slug's event stream.
 * Includes the Bearer token as a query param for EventSource (which cannot
 * set custom headers). Only used when TOKEN is set.
 */
export function buildEventsUrl(slug: string): string {
  const base = `${BASE_URL}/api/applications/${encodeURIComponent(slug)}/events?verbosity=verbose`;
  if (TOKEN) {
    return `${base}&token=${encodeURIComponent(TOKEN)}`;
  }
  return base;
}

/**
 * Redact secrets from any string before it lands in the DOM, clipboard, logs,
 * or any other user-visible surface. Catches:
 *   - `?token=<…>` / `&token=<…>` query params (used by EventSource)
 *   - `Bearer <…>` Authorization-header echoes
 *   - the literal current API token (defense-in-depth, in case env-var contents
 *     somehow get serialized into a string sent to the UI)
 *
 * Anything matched is replaced with a `[redacted]` placeholder. Idempotent —
 * safe to call on already-redacted strings.
 */
export function redactSensitive(text: string): string {
  let out = text.replace(/([?&]token=)[^\s&"'<>]+/gi, '$1[redacted]');
  // Bearer credentials are typically base64 / base64url, which can include
  // `+`, `/`, `=`, `~`, etc. Match up to the next whitespace, quote, or angle
  // bracket — not a narrow alphanumeric class — so we never leave a trailing
  // suffix in the rendered string. Case-insensitive to also catch `bearer …`.
  out = out.replace(/(Bearer\s+)[^\s"'<>]+/gi, '$1[redacted]');
  if (TOKEN && TOKEN.length >= 8) {
    // Escape regex metacharacters in the token before matching it as a literal.
    const escaped = TOKEN.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    out = out.replace(new RegExp(escaped, 'g'), '[redacted]');
  }
  return out;
}
