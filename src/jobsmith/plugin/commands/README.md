# Commands

Slash commands that users invoke from Claude Code.

**Status:** Pending extraction from `shakestzd/.claude/commands/apply.md` and `apply-batch.md`. See [ROADMAP.md](../ROADMAP.md).

## Planned commands

| Command | Purpose |
|---|---|
| `/apply <url-or-jd>` | Run the full pipeline against one job description |
| `/apply-batch <linkedin-search-url>` | Triage a LinkedIn search URL or list of job IDs and queue /apply runs for the top picks |
| `/jobsmith-init` | Scaffold a fresh master YAML in the user's application repo |

## Why slash commands

Claude Code plugin commands are the natural entry point for users. They produce a consistent dispatch path (read agent file → invoke orchestrator) and surface in the user's `/help` listing.
