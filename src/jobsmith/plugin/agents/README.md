# Agents

The 13 specialist agents that the orchestrator dispatches.

**Status:** Pending extraction from `shakestzd/.claude/agents/apply-*.md`. See [ROADMAP.md](../ROADMAP.md).

## Architecture

The /apply pipeline is a hybrid sequential-and-parallel dispatch:

1. `apply-jd-parser` — parse JD URL or text into structured fields
2. **Fan-out (parallel):** `apply-fit-scorer`, `apply-hm-enricher`, `apply-bullet-selector`
3. `anchor-bullet-guard` (Python script, not an agent)
4. `apply-relevance-inquirer` (conditional — only when anchors drop or must-haves are uncovered)
5. `apply-prose-writer` ↔ `apply-prose-qa` (loops up to 3 iterations)
6. `apply-resume-renderer`
7. `apply-portfolio-ats-checker`
8. `apply-visual-layout-reviewer` (loops up to 2 re-render iterations)
9. `apply-cover-letter-writer` (parallel branch)
10. `apply-index-writer` → `apply-db-logger`

The contract surface is in `apply/specialist-contracts.yaml` — frozen schema. No specialist may diverge from its declared input/output shape, tool allowlist, or model assignment without updating the contract first.

## Why second-person prompts

Each `apply-*.md` is written in second-person ("you read X, you write Y") so Claude Code can dispatch them via the Task tool with `subagent_type=apply-*`. Anthropic's plugin agent contract requires this voice.
