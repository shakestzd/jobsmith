"""/api/doctor router — run preflight environment checks.

Endpoints
---------
GET /doctor   Run all checks and return results as a list of DoctorCheckResult.

The endpoint is idempotent (read-only) so GET is the correct verb.
Auth is enforced via the top-level include_router dependency in main.py.
"""

from __future__ import annotations

from fastapi import APIRouter

from jobsmith.doctor import run_all_checks

from .schemas.doctor import DoctorCheckResult

router = APIRouter(tags=["doctor"])


def _map_status(ok: bool) -> str:
    """Map a CheckResult.ok bool to the API status string."""
    return "pass" if ok else "fail"


@router.get("/doctor", response_model=list[DoctorCheckResult])
def get_doctor() -> list[DoctorCheckResult]:
    """Run all preflight checks and return the results.

    Always returns HTTP 200 with the full list — callers inspect ``status``
    per item to determine if action is required.
    """
    results = run_all_checks()
    return [
        DoctorCheckResult(
            name=r.name,
            status=_map_status(r.ok),
            message=r.message,
        )
        for r in results
    ]


__all__ = ["router"]
