"""HTTP client for the jobsmith API (stub — slice 3 will implement).

Auth header convention:
    Authorization: Bearer <token>

All requests must include the above header with the token obtained from
``JOBSMITH_API_TOKEN`` env var or ``private/jobsmith.token`` file.

Example (slice 3 will flesh this out)::

    import os
    import httpx

    token = os.environ["JOBSMITH_API_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get("http://127.0.0.1:8000/api/master", headers=headers)
"""

from __future__ import annotations

# Slice 3 (feat-tbd) will implement JobsmithClient here.
# The authorization header convention is:
#   Authorization: Bearer <token>
