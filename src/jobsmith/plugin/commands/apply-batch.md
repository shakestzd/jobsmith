---
description: Triage a LinkedIn job-search URL or list of job IDs and queue /apply runs for the top picks. YOLO-friendly.
---

# Apply Batch

Run `date` first to get the exact current date.

## Inputs
$ARGUMENTS may contain:
- A LinkedIn job-search URL (extract `originToLandingJobPostings` IDs OR `currentJobId` if that's all that's there).
- A comma-separated list of LinkedIn job IDs (e.g. `4406570442,4405190558`).
- Optional flags: `--limit N` (default 2) caps how many top picks get an `/apply` dispatch.

If $ARGUMENTS is empty, ask the user for a URL or ID list. Don't guess.

## Workflow

### Step 1 — Parse the batch
Extract the list of LinkedIn job IDs. If the URL has `originToLandingJobPostings=...`, those are the batch. Otherwise fall back to `currentJobId`. Cap at 8 IDs to keep triage bounded.

### Step 2 — Pull each JD via Chrome MCP
The user is already logged into LinkedIn in Chrome. Use `mcp__claude-in-chrome__*` tools — do NOT launch a fresh browser. For each job ID, navigate to `https://www.linkedin.com/jobs/view/{id}/` and pull the rendered page text.

If a JD won't render (anti-bot, deleted, expired), record `unavailable` and move on. Don't retry more than once.

For each JD record at minimum:
- `id` (LinkedIn job ID)
- `company`
- `title`
- `location` + `work_mode` (Remote/Hybrid/On-site)
- `comp_band` (if visible)
- `posted_age` (e.g., "2d ago")
- `top_3_requirements` (bulleted, mechanical extraction — no analysis)
- `red_flags` (sponsorship-required, security-clearance-required, contract-only, etc.)

### Step 3 — Lightweight triage (mechanical, not opinionated)

Apply these filters in order. Drop a job if ANY of these hits:
1. Requires sponsorship (check the user's work authorization status — if they have a green card or full authorization, jobs that say "no sponsorship available" are FINE; only drop jobs that *require* sponsorship from the employer).
2. Requires active security clearance (TS/SCI etc.).
3. Pure foundation-model research with no infra / applied / climate angle.
4. Junior-only or internship-only.
5. JD couldn't be retrieved (unavailable).

Then score the remainder against the user's profile (`assets/content/work.yml`, `assets/content/skill.yml`):

| Signal | Weight |
|---|---|
| Climate / energy / sustainability / decarbonization | +3 |
| AI infrastructure / data centers / compute carbon | +3 |
| Optimization / OR / forecasting / scheduling | +2 |
| Geospatial / GIS / regulatory analytics | +2 |
| Python + SQL data engineering | +1 |
| ML/DS for ops, finance, or grid | +1 |
| Pure SWE with no analytics | -1 |
| Non-US (no remote-OK) | -2 |
| Senior IC / Staff (stretch but valid) | 0 |

Output a triage table with `id | company | title | work_mode | score | one-line fit/anti-fit note`. Sort descending by score.

### Step 4 — Recommend & confirm
Recommend the top `--limit` (default 2) jobs. State plainly which ones you'd skip and why.

Ask the user: **"Run /apply on these top picks now? (y / specific IDs / no)"**

Wait for their response. Don't auto-fire.

### Step 5 — Dispatch /apply per pick
For each approved job ID, in sequence (not parallel — `/apply` itself orchestrates parallel specialists):

1. Construct the LinkedIn job URL: `https://www.linkedin.com/jobs/view/{id}/`.
2. Invoke the `/apply` workflow by reading `agents/apply-agent.md` and following it precisely with that URL as the JD source.
3. `/apply` will pause at its own Step 3 (fit analysis review) — that's by design. The user confirms there per-application.
4. After each `/apply` completes, log the application slug + path. Then proceed to the next pick.

If a pick fails (resume render error, ATS check fail, etc.), record the failure and continue with the next pick. Do not block the batch on one failure.

### Step 6 — Final report
At end of session:
1. Triage table (all jobs, with drops noted).
2. Applications run, with paths to the rendered artifacts.
3. One-line "next batch" suggestion if the user wants to continue.

## Constraints (load-bearing)
- Never fabricate resume content. See the voice guide at ${VOICE_GUIDE_PATH}.
- Voice: explorer not marketer, thesis not product, specific not sweeping.
- Use `uv` for Python. Open PDFs with `open -a "Preview"`.
- Don't deliberate. Triage mechanically. Apply.

$ARGUMENTS
