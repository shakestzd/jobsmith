# /jobsmith-init

Scaffold a fresh application repo with master YAML stubs, `.apply-config.yaml`, and tracking dirs.

## What this does

Runs `scripts/jobsmith_init.py` against the user's current working directory. Creates:

- `assets/content/{work,skill,education,author,publication}.yml` — populated from the fictional Pat Doe sample (use `--no-examples` to write empty stubs instead)
- `.apply-config.yaml` — pointing at the above paths with sensible defaults
- `private/applications/` — output directory for /apply runs
- `private/capacity/profile.yaml` — used by `apply-fit-scorer` for evidence-weighted reasoning
- `.gitignore` additions — keep `.apply-state/`, rendered PDFs, and the SQLite job-search DB out of version control

## Invocation

From Claude Code in the directory where you want your application repo:

```
/jobsmith-init
```

By default, runs against the current working directory. Pass `--no-examples` to skip the Pat Doe sample and write empty stubs:

```
/jobsmith-init --no-examples
```

Pass `--force` to overwrite existing files (use with care):

```
/jobsmith-init --force
```

## After init

1. Edit `assets/content/*.yml` with your real work history, skills, education
2. Edit `.apply-config.yaml` — set `user.name`, `user.email`, etc.
3. Edit `private/capacity/profile.yaml` with your stack, specialties, years of experience
4. Run `/apply <job-url>` against a real role to generate your first tailored resume + cover letter

## Implementation

Reads `${CLAUDE_PLUGIN_ROOT}/scripts/jobsmith_init.py` and invokes it via `uv run python <path>` against the current directory.

```bash
uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/jobsmith_init.py "$PWD"
```
