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

// When served same-origin from the API (feat-9c980bef), resolve to the current
// origin so API calls work without hardcoded ports.  VITE_JOBSMITH_API_URL
// overrides this for split dev (Vite dev-server + separate uvicorn).
export const BASE_URL: string =
  import.meta.env.VITE_JOBSMITH_API_URL ?? window.location.origin;

// ---------------------------------------------------------------------------
// Runtime token (feat-16257e94 — localhost auto-auth)
// ---------------------------------------------------------------------------
//
// Resolution order (mutable, read at call-time so a 401 retry can update it):
//   1. window.__JOBSMITH__.token  — injected by the server on localhost binds.
//   2. VITE_JOBSMITH_API_TOKEN    — build-time env var (split-dev fallback).
//
// IMPORTANT: keep STATIC_TOKEN as a NAMED EXPORT so external callers that
// previously imported it continue to compile.  It is still used as the
// build-time fallback when the runtime shim is absent.
export const STATIC_TOKEN = import.meta.env.VITE_JOBSMITH_API_TOKEN ?? '';

/**
 * Return the best available static/shim bearer token at call-time.
 *
 * Reads window.__JOBSMITH__.token first (injected on localhost by staticui.py)
 * then falls back to the VITE build-time token.  Reading at call-time means a
 * hot-reload or future shim update takes effect without a full page reload.
 */
function getRuntimeToken(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const shim = (window as any).__JOBSMITH__;
    if (shim && typeof shim.token === 'string' && shim.token) {
      return shim.token;
    }
  } catch {
    // window not available (SSR / test environments without jsdom)
  }
  return STATIC_TOKEN;
}

const ACCESS_TOKEN_KEY = 'jobsmith.access_token';
const REFRESH_TOKEN_KEY = 'jobsmith.refresh_token';
export const JOBSMITH_DATA_CHANGED = 'jobsmith:data-changed';

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

export function getAccessToken(): string {
  return window.localStorage.getItem(ACCESS_TOKEN_KEY) ?? '';
}

export function hasStaticToken(): boolean {
  return Boolean(getRuntimeToken());
}

export function getRefreshToken(): string {
  return window.localStorage.getItem(REFRESH_TOKEN_KEY) ?? '';
}

export function setAuthTokens(accessToken: string, refreshToken: string): void {
  window.localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
  window.localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
}

export function clearAuthTokens(): void {
  window.localStorage.removeItem(ACCESS_TOKEN_KEY);
  window.localStorage.removeItem(REFRESH_TOKEN_KEY);
}

export function notifyDataChanged(path: string): void {
  window.dispatchEvent(new CustomEvent(JOBSMITH_DATA_CHANGED, { detail: { path } }));
}

export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = getAccessToken() || getRuntimeToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  return headers;
}

function authOnlyHeaders(): Record<string, string> {
  const token = getAccessToken() || getRuntimeToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export async function login(password: string): Promise<TokenPair> {
  const res = await fetch(`${BASE_URL}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  await throwIfError(res);
  const pair = await res.json() as TokenPair;
  setAuthTokens(pair.access_token, pair.refresh_token);
  notifyDataChanged('/api/auth/login');
  return pair;
}

export async function refreshAuth(): Promise<TokenPair> {
  const res = await fetch(`${BASE_URL}/api/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: getRefreshToken() }),
  });
  await throwIfError(res);
  const pair = await res.json() as TokenPair;
  setAuthTokens(pair.access_token, pair.refresh_token);
  notifyDataChanged('/api/auth/refresh');
  return pair;
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
    cache: 'no-store',
  });
  await throwIfError(res);
  return res.json() as Promise<T>;
}

export async function apiGetBlob(path: string): Promise<Blob> {
  const headers = authOnlyHeaders();
  const res = await fetch(`${BASE_URL}${path}`, { headers, cache: 'no-store' });
  await throwIfError(res);
  return res.blob();
}

export async function apiGetText(path: string): Promise<string> {
  const headers = authOnlyHeaders();
  const res = await fetch(`${BASE_URL}${path}`, { headers, cache: 'no-store' });
  await throwIfError(res);
  return res.text();
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  });
  await throwIfError(res);
  const data = await res.json() as T;
  notifyDataChanged(path);
  return data;
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
  const data = await res.json() as T;
  notifyDataChanged(path);
  return data;
}

export async function apiGetWithMeta<T>(
  path: string,
): Promise<{ data: T; etag: string | null; status: number }> {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: authHeaders(),
    cache: 'no-store',
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
  const data = await res.json() as T;
  notifyDataChanged(path);
  return data;
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
  options: { force?: boolean; jdText?: string; startFromPhase?: string } = {},
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
  if (options.startFromPhase) {
    body.start_from_phase = options.startFromPhase;
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
  // Use the mutable runtime token (from window.__JOBSMITH__ shim or VITE var)
  // so that SSE is authenticated on localhost even without VITE_JOBSMITH_API_TOKEN.
  const token = getAccessToken() || getRuntimeToken();
  if (token) {
    return `${base}&token=${encodeURIComponent(token)}`;
  }
  return base;
}

// ── Chat API ─────────────────────────────────────────────────────────────

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  created_at?: string;
}

export async function chatHistory(slug: string): Promise<ChatMessage[]> {
  const data = await apiGet<{ messages: ChatMessage[] }>(
    `/api/chat/history?slug=${encodeURIComponent(slug)}`,
  );
  return data.messages;
}

export async function chatResetSession(slug: string): Promise<void> {
  await apiPost('/api/chat/session/reset', { slug });
}

// ── Chat-proposed cover-letter edits (feat-fae0fda6) ──────────────────────

/** A structured asset-edit proposal emitted by the chat agent. */
export interface ChatProposal {
  asset: string;            // e.g. "cover_letter"
  slug: string;
  summary: string;
  rationale: string;
  new_content: string;      // COMPLETE revised draft
}

/** Result of applying a cover-letter proposal. */
export interface ApplyCoverLetterResult {
  applied: boolean;
  words?: number;
  render?: string;          // "ok" | "skipped" | "error: ..."
  rendered_path?: string;
  reason?: string;          // "fact_check_failed" on 422
  failed_claims?: string[]; // populated on 422
}

/** Fetch the current cover-letter-draft.md text (the "OLD" side of the diff). */
export async function getCoverLetterDraft(slug: string): Promise<string> {
  return apiGetText(
    `/api/applications/${encodeURIComponent(slug)}/documents/cover-letter-draft.md`,
  );
}

/**
 * Apply a chat-proposed cover-letter revision. Reads the JSON body for BOTH
 * success and the 422 fact-check-failure case so the caller can surface the
 * failed claims (we do not use apiPost here because it throws on 422 and would
 * discard the structured body).
 */
export async function applyCoverLetter(
  slug: string,
  newContent: string,
): Promise<ApplyCoverLetterResult> {
  const res = await fetch(
    `${BASE_URL}/api/applications/${encodeURIComponent(slug)}/cover-letter/apply`,
    {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ new_content: newContent }),
    },
  );
  let data: ApplyCoverLetterResult;
  try {
    data = (await res.json()) as ApplyCoverLetterResult;
  } catch {
    throw new JobsmithApiError(res.statusText || 'Apply failed', res.status);
  }
  // 422 = fact-check failure (structured body, not an exception). Any other
  // non-OK status is a genuine error.
  if (!res.ok && res.status !== 422) {
    const detail = (data as unknown as Record<string, unknown>).detail;
    throw new JobsmithApiError(formatDetail(detail, res.statusText), res.status);
  }
  notifyDataChanged(`/api/applications/${slug}/cover-letter`);
  return data;
}

/** Result of POST /api/applications/{slug}/cover-letter/render-pdf (feat-0e29138c). */
export interface RenderCoverLetterPdfResult {
  rendered: boolean;
  path?: string;   // "cover-letter.pdf" on success
  reason?: string; // "quarto_not_available" when quarto isn't installed
  error?: string;  // quarto stderr tail on a render failure
}

/**
 * On-demand: render the current cover-letter draft to a PDF. The cover letter
 * stays editable text; the PDF is produced ONLY by this explicit call (no
 * auto-render on apply). Returns a structured result for every non-404 outcome
 * (success, quarto-missing, render-error) so the caller can message the user.
 */
export async function renderCoverLetterPdf(
  slug: string,
): Promise<RenderCoverLetterPdfResult> {
  return apiPost<RenderCoverLetterPdfResult>(
    `/api/applications/${encodeURIComponent(slug)}/cover-letter/render-pdf`,
    {},
  );
}

// ── Postings API ─────────────────────────────────────────────────────────

import type { PostingRow, PostingPromoteResponse } from './types';

export interface PostingsFilter {
  status?: string;
  source?: string;
  specialty?: string;
  min_score?: number;
}

/** GET /api/postings — ranked list with optional filters. */
export async function getPostings(filter: PostingsFilter = {}): Promise<PostingRow[]> {
  const params = new URLSearchParams();
  if (filter.status) params.set('status', filter.status);
  if (filter.source) params.set('source', filter.source);
  if (filter.specialty) params.set('specialty', filter.specialty);
  if (filter.min_score != null) params.set('min_score', String(filter.min_score));
  const qs = params.toString() ? `?${params.toString()}` : '';
  return apiGet<PostingRow[]>(`/api/postings${qs}`);
}

/** POST /api/postings/{id}/status — transition posting status. */
export async function setPostingStatus(
  id: number,
  status: string,
): Promise<PostingRow> {
  return apiPost<PostingRow>(`/api/postings/${id}/status`, { status });
}

/** POST /api/postings/{id}/promote — promote to apply pipeline. */
export async function promotePosting(
  id: number,
): Promise<PostingPromoteResponse> {
  return apiPost<PostingPromoteResponse>(`/api/postings/${id}/promote`, {});
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
  const token = getAccessToken() || getRuntimeToken();
  if (token && token.length >= 8) {
    // Escape regex metacharacters in the token before matching it as a literal.
    const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    out = out.replace(new RegExp(escaped, 'g'), '[redacted]');
  }
  return out;
}
