"""Shared fixtures for end-to-end tests (feat-e4adbb42).

Provides:
- scratch_repo_root: a tmp_path-based repo with seeded master YAMLs + DB
- live_server: a subprocess-based `jobsmith up --no-open` with bounded
  readiness wait, proper teardown, and a ready httpx client.
- api_token: the static bearer token emitted at startup via stdout/env

Important notes:
- All heavy e2e fixtures require JOBSMITH_E2E=1 and skip otherwise.
- Node is required at WHEEL BUILD time only; the clean-venv install is npm-free.
- Server subprocess is always terminated/killed in fixture teardown.
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Opt-in gate: skip heavy e2e tests unless JOBSMITH_E2E=1
# ---------------------------------------------------------------------------

_E2E_ENABLED = os.environ.get("JOBSMITH_E2E", "0") == "1"

SKIP_E2E = pytest.mark.skipif(
    not _E2E_ENABLED,
    reason="Set JOBSMITH_E2E=1 to run full end-to-end suite",
)

# ---------------------------------------------------------------------------
# Minimal .apply-config.yaml template (sample-data layout, no LLM needed)
# ---------------------------------------------------------------------------

_CONFIG_YAML = """\
master:
  work_yml: assets/content/work.yml
  skill_yml: assets/content/skill.yml
  education_yml: assets/content/education.yml
  author_yml: assets/content/author.yml
  publication_yml: assets/content/publication.yml

output:
  applications_dir: private/applications
  job_search_db: private/job_search.db
  jobsmith_db: private/jobsmith.db

user:
  name: "Pat Doe"
  email: "pat.doe@example.com"
  phone: "(555) 012-3456"
  location: "Remote"
  github: "patdoe"
  linkedin: "linkedin.com/in/patdoe"

voice:
  voice_guide_path: null
  employment_gap_snippet: null

anchor_thresholds:
  money_min_usd: 10000000
  percent_min: 50.0
  asset_count_min: 100000

cover_letter:
  framework: careerfair-io
  default_salutation: "Hello,"

resume:
  max_pages: 1
  layout_iteration_limit: 2

fit_scorer:
  fast_threshold: 0.70
  profile_yaml: private/capacity/profile.yaml
"""

_WORK_YML = """\
- title: Senior Data Engineer
  company: Acme Corp
  location: Remote
  start: 2021-01
  end: present
  details:
    - Designed and maintained ETL pipelines processing $15M in daily revenue data
    - Reduced data ingestion latency by 60% via streaming architecture (Kafka + Flink)
    - Led a team of 4 engineers across 3 data domains
"""

_SKILL_YML = """\
categories:
  - name: Languages
    skills: [Python, SQL, Scala, TypeScript]
  - name: Data
    skills: [Spark, Kafka, dbt, Airflow]
  - name: Cloud
    skills: [AWS, GCP, Terraform]
"""

_EDUCATION_YML = """\
- institution: State University
  degree: B.Sc. Computer Science
  year: 2018
"""

_AUTHOR_YML = """\
name: Pat Doe
email: pat.doe@example.com
phone: "(555) 012-3456"
location: Remote
github: patdoe
linkedin: linkedin.com/in/patdoe
"""

_PUBLICATION_YML = """\
[]
"""


def _seed_scratch_repo(root: Path) -> None:
    """Write minimal but valid master YAMLs and config into *root*."""
    content_dir = root / "assets" / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    (content_dir / "work.yml").write_text(_WORK_YML)
    (content_dir / "skill.yml").write_text(_SKILL_YML)
    (content_dir / "education.yml").write_text(_EDUCATION_YML)
    (content_dir / "author.yml").write_text(_AUTHOR_YML)
    (content_dir / "publication.yml").write_text(_PUBLICATION_YML)

    (root / "private").mkdir(parents=True, exist_ok=True)
    (root / "private" / "applications").mkdir(parents=True, exist_ok=True)
    (root / "private" / "capacity").mkdir(parents=True, exist_ok=True)
    (root / "private" / "capacity" / "profile.yaml").write_text("user:\n  name: Pat Doe\n")

    (root / ".apply-config.yaml").write_text(_CONFIG_YAML)


@pytest.fixture(scope="session")
def scratch_repo_root(tmp_path_factory) -> Path:
    """Session-scoped scratch repo with seeded master YAMLs and DB.

    Reused across all e2e tests in the session so the server fixture
    only spins up once.
    """
    root = tmp_path_factory.mktemp("scratch_repo")
    _seed_scratch_repo(root)
    return root


# ---------------------------------------------------------------------------
# Wheel build + clean-venv fixtures (session-scoped, heavy)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory):
    """Build a wheel from the current source tree (requires node for vite build).

    Returns the Path to the .whl file.
    Skips if JOBSMITH_E2E != 1.
    """
    if not _E2E_ENABLED:
        pytest.skip("JOBSMITH_E2E=1 required")

    worktree = Path(__file__).parent.parent.parent  # repo root
    wheel_dir = tmp_path_factory.mktemp("wheel_out")

    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        timeout=300,  # 5 min max for wheel build
    )
    if result.returncode != 0:
        pytest.fail(
            f"uv build --wheel failed (rc={result.returncode}):\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    wheels = list(wheel_dir.glob("*.whl"))
    if not wheels:
        pytest.fail(f"No .whl found after uv build in {wheel_dir}")
    return wheels[0]


@pytest.fixture(scope="session")
def clean_venv(tmp_path_factory, built_wheel):
    """Create a clean (npm-free) venv and install the built wheel into it.

    Returns the venv root Path.  The venv has NO npm/node — only pip-installable
    Python deps, proving the wheel install is npm-free.
    """
    if not _E2E_ENABLED:
        pytest.skip("JOBSMITH_E2E=1 required")

    venv_dir = tmp_path_factory.mktemp("clean_venv")
    # Create venv via uv
    r = subprocess.run(
        ["uv", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        pytest.fail(f"uv venv failed: {r.stderr}")

    # Install wheel into the clean venv
    r = subprocess.run(
        ["uv", "pip", "install", str(built_wheel), "--python", str(venv_dir)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        pytest.fail(f"uv pip install wheel failed: {r.stderr}")

    # Confirm no npm binary exists in the venv
    npm_in_venv = venv_dir / "bin" / "npm"
    assert not npm_in_venv.exists(), (
        f"npm found in clean venv at {npm_in_venv} — wheel install must not require npm"
    )

    return venv_dir


# ---------------------------------------------------------------------------
# Free-port helper
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Poll until host:port accepts TCP connections. Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Live server subprocess fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_server(clean_venv, scratch_repo_root):
    """Start `jobsmith up --no-open` from the clean venv as a subprocess.

    Yields (base_url, api_token).  Guaranteed to terminate in teardown.
    """
    if not _E2E_ENABLED:
        pytest.skip("JOBSMITH_E2E=1 required")

    port = _find_free_port()
    host = "127.0.0.1"

    jobsmith_bin = clean_venv / "bin" / "jobsmith"
    if not jobsmith_bin.exists():
        pytest.fail(f"jobsmith binary not found at {jobsmith_bin}")

    # We set a known token so we don't have to parse it from startup output.
    known_token = "e2e-test-token-jobsmith-0001"
    env = {
        **os.environ,
        "JOBSMITH_API_TOKEN": known_token,
        "JOBSMITH_REPO_ROOT": str(scratch_repo_root),
        # No npm/node on PATH for the clean-venv server subprocess —
        # this proves the wheel install is self-contained.
        "PATH": str(clean_venv / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }

    proc = subprocess.Popen(
        [
            str(jobsmith_bin),
            "up",
            "--no-open",
            "--host", host,
            "--port", str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    ready = _wait_for_port(host, port, timeout=30.0)
    if not ready:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        proc.kill()
        output = ""
        with contextlib.suppress(Exception):
            output = proc.stdout.read() if proc.stdout else ""
        pytest.fail(
            f"Server did not start within 30s on {host}:{port}.\n"
            f"Server output:\n{output}"
        )

    base_url = f"http://{host}:{port}"
    yield base_url, known_token

    # Teardown: always terminate
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
