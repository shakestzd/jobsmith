// client.ts — Thin fetch-based HTTP client for the jobsmith API.
//
// Exports:
//   JobsmithApiError  — typed error class; callers check `.status`
//   apiGet            — authenticated GET, returns parsed JSON
//   apiPost           — authenticated POST with JSON body, returns parsed JSON
//   apiPut            — authenticated PUT with JSON body, returns parsed JSON
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

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'PUT',
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
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
  options: { force?: boolean } = {},
): Promise<ApplicationCreated> {
  return apiPost<ApplicationCreated>('/api/applications', {
    url,
    slug,
    force: options.force === true,
  });
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
