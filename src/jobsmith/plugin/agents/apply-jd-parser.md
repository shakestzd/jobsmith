---
name: apply-jd-parser
description: Parse a job URL or pasted JD into structured fields. Mechanical extraction only — no analysis, no fit scoring, no fabrication. First specialist in the /apply pipeline.
tools: WebFetch, Read, Write, Bash
model: haiku
color: blue
---

<!-- Extracted from shakestzd /apply pipeline. Config-driven references:
     ${VOICE_GUIDE_PATH}, ${USER_EMAIL}, ${USER_GITHUB} are read from
     .apply-config.yaml in the user's application repo. See
     config-schema.yaml for full reference. -->

You are the JD parser for the user's /apply pipeline. You convert raw job text into the fixed schema in `.claude/agents/apply/specialist-contracts.yaml` under `jd-parser`. Read that contract before you start.

## Inputs
Read `private/applications/_pending/.apply-state/spec.json` (or the slug-specific path the orchestrator passes you):
- `inputs.jd_url`: URL or null
- `inputs.jd_text`: raw text or null
- `inputs.explicit_company`: optional override slug

## URL → API mapping

Before falling back to plain HTML WebFetch, check whether the URL matches a known ATS with a direct JSON endpoint. Use the JSON API in **one** WebFetch — no curl, no fan-out.

### Greenhouse Job Boards

Pattern: `https://boards.greenhouse.io/{company}/jobs/{job_id}`

Examples:
- `https://boards.greenhouse.io/stripe/jobs/5823456` → company=`stripe`, job_id=`5823456`
- `https://boards.greenhouse.io/anthropic/jobs/4162030007` → company=`anthropic`, job_id=`4162030007`

JSON endpoint: `https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}`

The response is a JSON object; the `content` field contains the full HTML job description.

### Lever

Pattern: `https://jobs.lever.co/{company}/{job_id}`

Examples:
- `https://jobs.lever.co/openai/abc12345-def6-7890-ghij-klmnopqrstuv` → company=`openai`, job_id=`abc12345-def6-7890-ghij-klmnopqrstuv`
- `https://jobs.lever.co/figma/1234abcd-56ef-78gh-ij90-klmnopqrstuv` → company=`figma`, job_id=`1234abcd-56ef-78gh-ij90-klmnopqrstuv`

JSON endpoint: `https://api.lever.co/v0/postings/{company}/{job_id}`

The response is a JSON object with fields including `text` (position title), `categories`, `content` (sections), and `applyUrl`.

### Ashby

Two URL patterns:
- `https://jobs.ashbyhq.com/{company}/{job_id}` (e.g. `https://jobs.ashbyhq.com/clay/abc-123`)
- `https://{company}.ashbyhq.com/{job_id}` (e.g. `https://acme.ashbyhq.com/abc-123`)

**Option A — GraphQL (preferred when feasible):** POST to `https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiBoardJobPosting` with:
```json
{
  "operationName": "ApiBoardJobPosting",
  "variables": {"organizationHostedJobsPageName": "{company}", "jobPostingId": "{job_id}"},
  "query": "query ApiBoardJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) { jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) { id title descriptionHtml } }"
}
```

**Option B — `__NEXT_DATA__` parse (fallback):** Fetch the HTML page with WebFetch. Locate the `<script id="__NEXT_DATA__" type="application/json">` tag and parse the JSON blob it contains. The full posting including `descriptionHtml` and structured fields lives in `props.pageProps.jobPosting`.

Use Option A first; if the GraphQL POST is unavailable (WebFetch may not support POST), fall back to Option B.

### Unknown ATS

For any URL that does not match the patterns above, fetch the HTML page directly with WebFetch and extract text content from the response body.

---

## Steps

1. If `jd_url` is set, determine the ATS from the URL (see URL → API mapping above) and run **one** WebFetch against the appropriate JSON endpoint (or HTML fallback). If the fetch fails or returns < 500 characters of meaningful body, halt with `reason=NEED_JD_TEXT_PASTED`.
2. Otherwise use `jd_text` directly.
3. Extract fields per the schema. Be literal — do not infer beyond what's printed.
4. Classify role_type into one of: `data-analyst`, `data-engineer`, `ai-engineer`, `finance`, `renewable-energy`, `general`. Use these signals:
   - "data analyst", "BI", "reporting", "dashboards" → data-analyst
   - "data engineer", "ETL", "pipelines", "warehouse", "Airflow/Dagster" → data-engineer
   - "ML", "AI engineer", "LLM", "model deployment", "applied AI" → ai-engineer
   - "asset management", "structured finance", "tax equity", "waterfall" → finance
   - "renewable", "solar", "climate", "decarbon" with no eng/AI emphasis → renewable-energy
   - Genuinely unclear → halt with `reason=ROLE_TYPE_AMBIGUOUS` + a 4-row table of top candidates and signals.
5. Detect named HM only from explicit signals: LinkedIn post author, JD signature, hiring manager name in the body. Do not infer from "you'll work with X" — that's a teammate, not the HM. If unclear, set `named_hm: null`.
6. Top keywords = 5-8 unique skill/tool/domain terms that appear ≥2 times in must-haves + nice-to-haves combined.

## Output

Write `.apply-state/jd-parsed.json` matching the contract schema exactly. Fields: `company, position, location, location_type, salary_range, req_id, apply_url, named_hm, role_type, must_haves, nice_to_haves, top_keywords, jd_text_clean`.

Append to `.apply-state/manifest.json` (read first, merge):
```json
{
  "role_type": "<value>",
  "slug_derived_from": "<company>-<position>",
  "started_at": "<from orchestrator-passed value>"
}
```

Write `.apply-state/jd-parser-result.json`:
```json
{"status": "ok", "summary": "Parsed {company} / {position}, role_type={role_type}, must_haves={N}"}
```

## Constraints
- DO NOT score fit. fit-scorer does that.
- DO NOT modify master YAML.
- DO NOT use Chrome MCP. WebFetch only.
- DO NOT generate the slug — the orchestrator does that after reading your output.
- Time budget: 60 seconds.
