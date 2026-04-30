---
description: Scaffold a tailored job application from a URL or pasted job description
---

# Apply to Job

Run `date` first to get the exact current date.

Read the application workflow agent instructions from `agents/apply-agent.md` and follow them precisely.

If $ARGUMENTS contains a URL, fetch the job description from that URL using WebFetch.
If $ARGUMENTS contains pasted text, treat it as the job description.
If no arguments, ask: "Paste a job URL or the full job description text."

Execute the full workflow defined in the agent file. Present the fit analysis and strategy BEFORE generating files — get the user's confirmation to proceed.

Remember: The goal is a tailored, high-quality application in 25 minutes, not a perfect one in 3 hours.

$ARGUMENTS
