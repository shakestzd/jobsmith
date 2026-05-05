"""/api/config router — read, validate, and write `.apply-config.yaml`.

Endpoints
---------
GET  /config          Load and return parsed config as JSON.
POST /config/validate Validate body (YAML or JSON) without saving.
PUT  /config          Validate body then write `.apply-config.yaml` if valid.

Auth is enforced via the top-level include_router dependency in main.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError as PydanticValidationError

from jobsmith.config import CONFIG_FILENAME, JobsmithConfig, load_config

from .schemas.config import ConfigValidateResponse, ValidationError

router = APIRouter(tags=["config"])


def _parse_body(body: bytes) -> dict[str, Any]:
    """Parse raw request body as YAML (which is a superset of JSON).

    Raises HTTPException(400) for any client-side parse error so we never
    surface a 500 (roborev job 940 finding).
    """
    try:
        data = yaml.safe_load(body.decode("utf-8") if body else "")
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML/JSON: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Body is not valid UTF-8: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail="Body must be a YAML/JSON object (mapping)",
        )
    return data


def _validate_config_data(data: dict[str, Any]) -> tuple[JobsmithConfig | None, list[ValidationError]]:
    """Validate config data dict. Returns (config, []) on success or (None, errors)."""
    try:
        config = JobsmithConfig.model_validate(data)
        return config, []
    except PydanticValidationError as exc:
        errors = [
            ValidationError(
                field=".".join(str(loc) for loc in err["loc"]) if err["loc"] else "root",
                message=err["msg"],
            )
            for err in exc.errors()
        ]
        return None, errors


@router.get("/config", response_model=dict[str, Any])
def get_config() -> dict[str, Any]:
    """Load `.apply-config.yaml` from the current working directory and return it.

    Returns defaults if no config file is found.
    """
    config = load_config()
    return config.model_dump(mode="json")


@router.post("/config/validate", response_model=ConfigValidateResponse)
async def validate_config(request: Request) -> ConfigValidateResponse:
    """Validate a config payload (YAML or JSON) without saving.

    Always returns HTTP 200 — inspect ``ok`` to determine validity.
    """
    body = await request.body()
    data = _parse_body(body)
    _, errors = _validate_config_data(data)
    return ConfigValidateResponse(ok=len(errors) == 0, errors=errors)


@router.put("/config", response_model=dict[str, Any])
async def put_config(request: Request) -> dict[str, Any]:
    """Validate then persist config to `.apply-config.yaml`.

    Returns 422 with ``errors`` if validation fails (no file written).
    Returns 200 with the saved config on success.
    """
    body = await request.body()
    data = _parse_body(body)
    config, errors = _validate_config_data(data)
    if errors:
        raise HTTPException(
            status_code=422,
            detail=[{"field": e.field, "message": e.message} for e in errors],
        )
    config_path = Path.cwd() / CONFIG_FILENAME
    with config_path.open("w") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    return config.model_dump(mode="json")  # type: ignore[union-attr]


__all__ = ["router"]
