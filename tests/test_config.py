"""Tests for jobsmith.config — JobsmithConfig Pydantic loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobsmith.config import (
    BenchmarkConfig,
    JobsmithConfig,
    find_config,
    load_config,
)


def test_default_config_loads() -> None:
    """Empty config → all defaults apply."""
    config = JobsmithConfig()
    assert config.master.work_yml == Path("assets/content/work.yml")
    assert config.anchor_thresholds.money_min_usd == 10_000_000
    assert config.cover_letter.framework == "careerfair-io"


def test_load_config_from_file(tmp_path: Path) -> None:
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "user": {"name": "Pat Doe", "email": "pat@example.com"},
                "anchor_thresholds": {"money_min_usd": 5_000_000},
            }
        )
    )
    config = load_config(config_file)
    assert config.user.name == "Pat Doe"
    assert config.user.email == "pat@example.com"
    assert config.anchor_thresholds.money_min_usd == 5_000_000
    # Unset fields fall back to defaults
    assert config.anchor_thresholds.percent_min == 50.0


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nonexistent.yaml")
    assert config == JobsmithConfig()


def test_find_config_walks_up(tmp_path: Path) -> None:
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("user:\n  name: Pat\n")
    nested = tmp_path / "deep" / "nested" / "dir"
    nested.mkdir(parents=True)
    found = find_config(nested)
    assert found == config_file


def test_find_config_returns_none_when_absent(tmp_path: Path) -> None:
    assert find_config(tmp_path) is None


def test_anchor_threshold_percent_validation() -> None:
    """Percent must be 0-100."""
    with pytest.raises(ValueError, match="percent_min must be 0-100"):
        JobsmithConfig.model_validate(
            {"anchor_thresholds": {"percent_min": 150.0}}
        )


def test_voice_settings_default_banned_lists() -> None:
    config = JobsmithConfig()
    assert "Architected" in config.voice.banned_action_verbs
    assert "enterprise" in config.voice.banned_buzzwords
    assert "perfect fit" in config.voice.banned_marketer_phrases


def test_user_identity_optional() -> None:
    """User fields default to empty strings — never None — for safe templating."""
    config = JobsmithConfig()
    assert config.user.name == ""
    assert config.user.email == ""


# ---------------------------------------------------------------------------
# BenchmarkConfig tests
# ---------------------------------------------------------------------------

def test_benchmark_config_default_is_empty_not_required() -> None:
    config = JobsmithConfig()
    assert config.benchmarks.required is False
    assert config.benchmarks.resume_pdf is None
    assert config.benchmarks.resume_qmd is None
    assert config.benchmarks.cover_letter_md is None
    assert config.benchmarks.cover_letter_pdf is None
    assert config.benchmarks.workflow_html is None


def test_benchmark_config_round_trip(tmp_path: Path) -> None:
    """Config with benchmarks: section parses correctly."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        "benchmarks:\n"
        "  resume_qmd: private/benchmarks/resume.qmd\n"
        "  cover_letter_md: private/benchmarks/cover-letter.md\n"
        "  required: true\n"
    )
    config = load_config(config_file)
    assert config.benchmarks.resume_qmd == Path("private/benchmarks/resume.qmd")
    assert config.benchmarks.cover_letter_md == Path("private/benchmarks/cover-letter.md")
    assert config.benchmarks.required is True
    # Unset fields remain None
    assert config.benchmarks.resume_pdf is None
    assert config.benchmarks.workflow_html is None


def test_benchmark_config_required_true_missing_path_still_valid_config() -> None:
    """required=true with missing file path is still a valid Pydantic model.

    The validation happens at doctor/use time, not at parse time.
    """
    config = JobsmithConfig.model_validate(
        {
            "benchmarks": {
                "resume_qmd": "/nonexistent/path/resume.qmd",
                "required": True,
            }
        }
    )
    assert config.benchmarks.required is True
    assert config.benchmarks.resume_qmd == Path("/nonexistent/path/resume.qmd")


def test_benchmark_config_all_fields_optional() -> None:
    """Every benchmark path field can be set or left None independently."""
    bm = BenchmarkConfig(
        resume_pdf=Path("a.pdf"),
        cover_letter_pdf=Path("cl.pdf"),
    )
    assert bm.resume_pdf == Path("a.pdf")
    assert bm.cover_letter_pdf == Path("cl.pdf")
    assert bm.resume_qmd is None
    assert bm.cover_letter_md is None
    assert bm.workflow_html is None


# ---------------------------------------------------------------------------
# LLMSettings tests
# ---------------------------------------------------------------------------

from jobsmith.config import ApplySettings, LLM_PRESETS, LLMSettings, NodeBackendConfig  # noqa: E402


def test_llm_default_provider_is_claude_cli() -> None:
    """No llm block in config → provider defaults to claude_cli."""
    config = JobsmithConfig()
    assert config.llm.provider == "claude_cli"


def test_llm_default_fields() -> None:
    """Default LLMSettings has safe nulls and sensible numeric defaults."""
    settings = LLMSettings()
    assert settings.provider == "claude_cli"
    assert settings.model is None
    assert settings.base_url is None
    assert settings.api_key is None
    assert settings.budget_usd > 0
    assert settings.timeout_s > 0


def test_llm_block_parses_from_config_dict() -> None:
    """llm fields load from model_validate with a full dict."""
    config = JobsmithConfig.model_validate(
        {
            "llm": {
                "provider": "openai_compatible",
                "model": "mlx-community/Llama-3.2-3B",
                "base_url": "http://127.0.0.1:8080/v1",
                "api_key": "not-used",
                "budget_usd": 0.5,
                "timeout_s": 120,
            }
        }
    )
    assert config.llm.provider == "openai_compatible"
    assert config.llm.model == "mlx-community/Llama-3.2-3B"
    assert config.llm.base_url == "http://127.0.0.1:8080/v1"
    assert config.llm.api_key == "not-used"
    assert config.llm.budget_usd == 0.5
    assert config.llm.timeout_s == 120


def test_llm_block_round_trips_from_yaml_file(tmp_path: Path) -> None:
    """llm section parses correctly from a .apply-config.yaml file."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        "llm:\n"
        "  provider: openai_compatible\n"
        "  base_url: http://localhost:11434/v1\n"
        "  model: llama3.2\n"
    )
    config = load_config(config_file)
    assert config.llm.provider == "openai_compatible"
    assert config.llm.base_url == "http://localhost:11434/v1"
    assert config.llm.model == "llama3.2"


def test_llm_mlx_preset_base_url() -> None:
    """MLX preset resolves to the correct local base_url."""
    assert LLM_PRESETS["mlx"] == "http://127.0.0.1:8080/v1"


def test_llm_ollama_preset_base_url() -> None:
    """Ollama preset resolves to the correct local base_url."""
    assert LLM_PRESETS["ollama"] == "http://localhost:11434/v1"


def test_llm_invalid_provider_rejected() -> None:
    """An unknown provider value is rejected by Pydantic validation."""
    with pytest.raises(ValueError):
        JobsmithConfig.model_validate({"llm": {"provider": "unknown_provider"}})


def test_llm_openai_compatible_without_base_url_rejected() -> None:
    """openai_compatible provider without base_url raises on validation."""
    with pytest.raises(ValueError, match="base_url"):
        JobsmithConfig.model_validate({"llm": {"provider": "openai_compatible"}})


def test_llm_cli_providers_do_not_require_base_url() -> None:
    """CLI providers are valid without a base_url."""
    for provider in ("claude_cli", "antigravity_cli", "codex_cli"):
        config = JobsmithConfig.model_validate({"llm": {"provider": provider}})
        assert config.llm.provider == provider
        assert config.llm.base_url is None


def test_llm_no_llm_block_backward_compat(tmp_path: Path) -> None:
    """Config file with no llm block uses defaults — backward compat preserved."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("user:\n  name: Pat Doe\n")
    config = load_config(config_file)
    assert config.llm.provider == "claude_cli"
    assert config.user.name == "Pat Doe"


# ---------------------------------------------------------------------------
# LLMSettings.apply (ApplySettings) tests — TDD slice feat-c7fab5c0
# ---------------------------------------------------------------------------


def test_apply_orchestrator_defaults_to_claude_cloud() -> None:
    """When no llm.apply block is present, orchestrator defaults to claude_cloud."""
    config = JobsmithConfig()
    assert config.llm.apply.orchestrator == "claude_cloud"


def test_no_apply_block_existing_behavior_unchanged(tmp_path: Path) -> None:
    """Config with no llm.apply section → apply block is at default claude_cloud."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text("llm:\n  provider: claude_cli\n")
    config = load_config(config_file)
    assert config.llm.apply.orchestrator == "claude_cloud"
    assert config.llm.apply.node_backend is None
    assert config.llm.apply.node_backends == {}


def test_apply_code_local_orchestrator_parses() -> None:
    """code_local is a valid orchestrator value."""
    config = JobsmithConfig.model_validate(
        {"llm": {"apply": {"orchestrator": "code_local"}}}
    )
    assert config.llm.apply.orchestrator == "code_local"


def test_apply_node_backend_default_parses() -> None:
    """apply.node_backend with a provider + model parses correctly."""
    config = JobsmithConfig.model_validate(
        {
            "llm": {
                "apply": {
                    "orchestrator": "code_local",
                    "node_backend": {
                        "provider": "openai_compatible",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "model": "mlx-community/gemma-3-4b",
                    },
                }
            }
        }
    )
    nb = config.llm.apply.node_backend
    assert nb is not None
    assert nb.provider == "openai_compatible"
    assert nb.base_url == "http://127.0.0.1:8080/v1"
    assert nb.model == "mlx-community/gemma-3-4b"


def test_apply_node_backends_map_parses() -> None:
    """apply.node_backends is a dict of node_name -> NodeBackendConfig."""
    config = JobsmithConfig.model_validate(
        {
            "llm": {
                "apply": {
                    "orchestrator": "code_local",
                    "node_backend": {
                        "provider": "openai_compatible",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "model": "gemma-3",
                    },
                    "node_backends": {
                        "prose-write": {
                            "provider": "anthropic",
                            "api_key": "sk-ant-test",
                            "model": "claude-3-5-haiku-20241022",
                        }
                    },
                }
            }
        }
    )
    overrides = config.llm.apply.node_backends
    assert "prose-write" in overrides
    assert overrides["prose-write"].provider == "anthropic"
    assert overrides["prose-write"].api_key == "sk-ant-test"
    assert overrides["prose-write"].model == "claude-3-5-haiku-20241022"


def test_apply_on_failure_defaults_to_error() -> None:
    """on_failure defaults to 'error' when not specified."""
    config = JobsmithConfig()
    assert config.llm.apply.on_failure == "error"


def test_apply_on_failure_fallback_cloud_valid() -> None:
    """fallback_cloud is a valid on_failure value."""
    config = JobsmithConfig.model_validate(
        {"llm": {"apply": {"on_failure": "fallback_cloud"}}}
    )
    assert config.llm.apply.on_failure == "fallback_cloud"


def test_apply_on_failure_invalid_rejected() -> None:
    """Unknown on_failure value is rejected by Pydantic."""
    with pytest.raises(ValueError):
        JobsmithConfig.model_validate(
            {"llm": {"apply": {"on_failure": "retry_forever"}}}
        )


def test_anthropic_provider_accepted_in_node_backend() -> None:
    """anthropic is now a valid LLMProvider for node backends."""
    nb = NodeBackendConfig(provider="anthropic", api_key="sk-ant-test")
    assert nb.provider == "anthropic"


def test_anthropic_no_api_key_rejected_in_node_backend() -> None:
    """anthropic provider without api_key raises a clear Pydantic error."""
    with pytest.raises(ValueError, match="api_key"):
        NodeBackendConfig(provider="anthropic")


def test_anthropic_provider_accepted_in_llm_settings() -> None:
    """anthropic is valid as the top-level llm.provider."""
    config = JobsmithConfig.model_validate(
        {"llm": {"provider": "anthropic", "api_key": "sk-ant-test"}}
    )
    assert config.llm.provider == "anthropic"


def test_apply_round_trip_from_yaml(tmp_path: Path) -> None:
    """Full llm.apply block round-trips through .apply-config.yaml."""
    config_file = tmp_path / ".apply-config.yaml"
    config_file.write_text(
        "llm:\n"
        "  provider: claude_cli\n"
        "  apply:\n"
        "    orchestrator: code_local\n"
        "    on_failure: fallback_cloud\n"
        "    node_backend:\n"
        "      provider: openai_compatible\n"
        "      base_url: http://127.0.0.1:8080/v1\n"
        "      model: gemma-3-4b\n"
        "    node_backends:\n"
        "      prose-write:\n"
        "        provider: anthropic\n"
        "        api_key: sk-ant-abc\n"
        "        model: claude-3-5-haiku-20241022\n"
    )
    config = load_config(config_file)
    assert config.llm.apply.orchestrator == "code_local"
    assert config.llm.apply.on_failure == "fallback_cloud"
    assert config.llm.apply.node_backend is not None
    assert config.llm.apply.node_backend.provider == "openai_compatible"
    assert config.llm.apply.node_backend.base_url == "http://127.0.0.1:8080/v1"
    assert config.llm.apply.node_backend.model == "gemma-3-4b"
    assert "prose-write" in config.llm.apply.node_backends
    pw = config.llm.apply.node_backends["prose-write"]
    assert pw.provider == "anthropic"
    assert pw.api_key == "sk-ant-abc"


def test_apply_node_backends_empty_by_default() -> None:
    """When node_backends is not set, it defaults to an empty dict."""
    settings = ApplySettings()
    assert settings.node_backends == {}


def test_apply_invalid_orchestrator_rejected() -> None:
    """An unknown orchestrator value is rejected by Pydantic."""
    with pytest.raises(ValueError):
        JobsmithConfig.model_validate(
            {"llm": {"apply": {"orchestrator": "kubernetes_local"}}}
        )
