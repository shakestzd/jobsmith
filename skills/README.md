# Skills

Claude Code skill modules invoked by users via `Skill` or auto-triggered via slash commands.

**Status:** Pending extraction. See [ROADMAP.md](../ROADMAP.md).

## Planned skills

- **`apply`** — full /apply pipeline orchestration (also dispatchable via `/apply` command)
- **`apply-batch`** — LinkedIn-search-URL triage + queue
- **`humanizer`** — AI-tell scrubber for cover-letter prose (based on Wikipedia's "Signs of AI writing" guide)
- **`prepare-application`** — interactive variant that walks the user through the pipeline with explicit pauses

## Why skills (not just agents)

Skills are the Claude Code abstraction for user-facing capabilities. Agents are dispatched by the orchestrator; skills are dispatched by the user (or by a slash command on the user's behalf). The /apply pipeline is exposed as both — the slash command is the convenient entry point, and the skill makes it discoverable in `/help`.
