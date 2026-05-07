"""jobsmith.core.paths — path resolution helpers (feat-55152c31, Slice 2b).

Pure helpers with no Rich/Click/Typer dependencies. Relocated from apply.py.
"""
from __future__ import annotations

import contextlib
from pathlib import Path


def build_paths(slug: str, cwd: Path, plugin_directory: Path) -> dict[str, str]:
    """Build the paths dict injected into each phase prompt.

    Called once per phase so that ``apply_state_dir`` always uses the
    current (possibly post-reconcile) slug.

    Returns a flat string→string mapping of absolute paths.  Optional
    master YAMLs (``publication_yml``) are omitted when not configured.
    When ``.apply-config.yaml`` cannot be found the dict contains only the
    plugin-side paths (agent still gets them).
    """
    from jobsmith.benchmarks import resolve_benchmark_or_fallback
    from jobsmith.config import find_config, load_config
    from jobsmith.paths import resolve

    config_path = find_config(cwd)

    result: dict[str, str] = {
        "plugin_dir": str(plugin_directory.resolve()),
        "agent_dir": str((plugin_directory / "agents").resolve()),
        "specialist_contracts": str(
            (plugin_directory / "agents" / "apply" / "specialist-contracts.yaml").resolve()
        ),
    }

    if config_path is not None:
        result["config"] = str(config_path.resolve())
        config = load_config(config_path)
        repo_root = config_path.parent

        # Master YAMLs — include only those that are configured (non-None)
        result["master.work_yml"] = str(resolve(config.master.work_yml, repo_root))
        result["master.skill_yml"] = str(resolve(config.master.skill_yml, repo_root))
        result["master.education_yml"] = str(resolve(config.master.education_yml, repo_root))
        result["master.author_yml"] = str(resolve(config.master.author_yml, repo_root))
        if config.master.publication_yml is not None:
            result["master.publication_yml"] = str(
                resolve(config.master.publication_yml, repo_root)
            )
        if config.master.award_yml is not None:
            result["master.award_yml"] = str(resolve(config.master.award_yml, repo_root))
        # Slice C: projects schema. Inject the raw path AND a filtered JSON
        # so the bullet-selector can include the projects already pre-filtered
        # (excluded_from_resume / excluded_kinds / is_project / homepage URL).
        # The pre-filter happens here rather than in the agent so the agent
        # never sees suppressed entries.
        if config.master.projects_yml is not None:
            projects_path = resolve(config.master.projects_yml, repo_root)
            if projects_path.exists():
                result["master.projects_yml"] = str(projects_path)

        # apply_state_dir — absolute path for the current slug
        apps_dir = resolve(config.output.applications_dir, repo_root)
        result["apply_state_dir"] = str(apps_dir / slug / ".apply-state")

        # Benchmark paths — resolve for the three specialists that consume them.
        # Falls back to Pat Doe files when user hasn't configured benchmarks.
        # Skip the key entirely when no benchmark is available (resolver returned
        # None) so we never inject a non-existent path. The bundled Pat Doe pack
        # only ships resume.qmd + cover-letter.md, so resume_pdf has no fallback;
        # specialists treat the absent key as "no benchmark available for this
        # field" rather than reading a missing file.
        # Raises BenchmarkRequiredError only when benchmarks.required=True and
        # the field is unset — in that case we propagate up to the caller.
        for field, key in (
            ("resume_qmd", "benchmark.resume_qmd"),
            ("cover_letter_md", "benchmark.cover_letter_md"),
            ("resume_pdf", "benchmark.resume_pdf"),
        ):
            path = resolve_benchmark_or_fallback(field, config, repo_root)
            if path is not None:
                result[key] = str(path)

        # Feedback directory — soft style lessons for prose-writer + cover-letter-writer.
        # Present only when the directory exists; absent key means "no feedback yet".
        feedback_dir = repo_root / "private" / "feedback"
        if feedback_dir.exists():
            result["feedback.dir"] = str(feedback_dir.resolve())

        # Voice profile (Slice B.1) — derived from benchmarks.resume_qmd by
        # voice.load_voice_profile() and cached at .apply-state/voice-profile.json.
        # tell-fixer / prose-writer / cover-letter-writer read banned_verbs /
        # banned_adjectives / result_verbs from this JSON instead of inlining
        # them. We compute the profile here so the cache is written before any
        # specialist runs; load_voice_profile() handles cache hit/miss internally.
        # Pass the already-resolved benchmark path so voice.py never has to
        # re-resolve relative to CWD (would silently miss the file when
        # `jobsmith apply` is invoked from a subdirectory).
        from jobsmith.voice import (
            load_voice_profile,  # local import — avoid circular at module load
        )
        voice_cache_dir = apps_dir / slug / ".apply-state"
        resolved_benchmark = result.get("benchmark.resume_qmd")
        # Voice profile is non-blocking: if computation fails (corrupt
        # benchmark, etc.), specialists fall back to seed defaults.
        with contextlib.suppress(Exception):
            load_voice_profile(
                config,
                cache_dir=voice_cache_dir,
                benchmark_path_override=Path(resolved_benchmark) if resolved_benchmark else None,
            )
        result["voice_profile_json"] = str(voice_cache_dir / "voice-profile.json")

        # Slice C: pre-filter projects.yml and emit projects-filtered.json so
        # bullet-selector consumes only entries that pass the kind / homepage /
        # excluded_from_resume / is_project filters. The agent never sees
        # suppressed entries — this prevents the Clay bug where the user's
        # portfolio site was wrongly listed as a project deliverable.
        if config.master.projects_yml is not None:
            projects_path = resolve(config.master.projects_yml, repo_root)
            if projects_path.exists():
                from jobsmith.assemble import load_projects
                # author.homepage may not be loaded yet; we resolve it best-effort
                # from author.yml so the URL-matches filter works.
                author_yml_path = resolve(config.master.author_yml, repo_root)
                author_homepage: str | None = None
                if author_yml_path.exists():
                    try:
                        import yaml as _yaml  # local — only here for one-shot read
                        ay = _yaml.safe_load(author_yml_path.read_text())
                        author = (ay or {}).get("author")
                        if isinstance(author, list) and author:
                            author = author[0]
                        if isinstance(author, dict):
                            author_homepage = (author.get("homepage") or "").strip() or None
                    except Exception:
                        author_homepage = None
                try:
                    # Pass the EXACT projects file path — load_projects accepts
                    # a file or directory. Earlier we passed parent which only
                    # worked for files literally named "projects.yml".
                    filtered = load_projects(
                        projects_path, config.resume, author_homepage
                    )
                    voice_cache_dir.mkdir(parents=True, exist_ok=True)
                    filtered_path = voice_cache_dir / "projects-filtered.json"
                    import json as _json
                    filtered_path.write_text(_json.dumps(filtered, indent=2))
                    result["projects_filtered_json"] = str(filtered_path)
                except Exception:
                    # Pre-filter is non-blocking; bullet-selector falls back
                    # to "no projects" when the key is absent.
                    pass

    return result


def apply_state_dir(slug: str, cwd: Path) -> Path | None:
    """Resolve the ``.apply-state`` directory for *slug* under the project root.

    Returns None if ``.apply-config.yaml`` cannot be found, so callers can
    silently skip operations that require the directory.
    """
    from jobsmith.config import find_config, load_config
    from jobsmith.paths import resolve

    config_path = find_config(cwd)
    if config_path is None:
        return None
    config = load_config(config_path)
    repo_root = config_path.parent
    return resolve(config.output.applications_dir, repo_root) / slug / ".apply-state"


def applications_dir(cwd: Path) -> Path | None:
    """Resolve the absolute ``applications/`` directory, or None if config absent."""
    from jobsmith.config import find_config, load_config
    from jobsmith.paths import resolve

    config_path = find_config(cwd)
    if config_path is None:
        return None
    config = load_config(config_path)
    repo_root = config_path.parent
    return resolve(config.output.applications_dir, repo_root)


def pipeline_db_path(cwd: Path) -> Path | None:
    """Resolve the pipeline DB absolute path under *cwd*.

    Returns ``None`` if config cannot be located. Does NOT verify the file
    exists — callers handle the "DB not yet created" case via ``get_state``
    which transparently materializes the schema.
    """
    from jobsmith.config import find_config, load_config

    config_path = find_config(cwd)
    if config_path is None:
        return None
    config = load_config(config_path)
    repo_root = config_path.parent
    return (repo_root / config.output.jobsmith_db).resolve()
