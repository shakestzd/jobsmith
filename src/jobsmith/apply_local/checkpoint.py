"""Per-node checkpoints for the LOCAL apply driver (feat-d67d2b1a, slice 1).

Each node persists its result to
``applications/{slug}/.apply-state/<name>.json`` — the EXISTING per-application
state directory declared in specialist-contracts.yaml. The slug is threaded
through every call; there is no bare ``.apply-state/`` path.

Resume policy (authoritative):

* Writes are ATOMIC: a sibling ``*.tmp`` file is written and ``os.replace``-d
  into place, so a crash mid-write can never leave a half-written checkpoint.
* Only a ``parse_ok=true`` checkpoint counts as present. A ``parse_ok=false``
  result is treated as ABSENT (the node must re-run on resume). The driver
  additionally only writes ``status=ok`` results, so a ``halt`` is never cached
  and is always re-evaluated on the next run.

The on-disk shape is the :class:`~jobsmith.apply_local.driver.NodeResult`
envelope (``name`` / ``status`` / ``parse_ok`` / ``reason`` / ``data``) so a
resume can reconstruct the full result, not just the payload.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_DIRNAME = ".apply-state"
_APPLICATIONS = "applications"


def _base_root(root: str | os.PathLike[str] | None) -> Path:
    """Resolve the directory under which ``applications/`` lives."""
    return Path(root) if root is not None else Path.cwd()


def apply_state_dir(slug: str, *, root: str | os.PathLike[str] | None = None) -> Path:
    """Return ``{root}/applications/{slug}/.apply-state`` for ``slug``."""
    if not slug:
        raise ValueError("apply_state_dir requires a non-empty slug.")
    return _base_root(root) / _APPLICATIONS / slug / STATE_DIRNAME


def checkpoint_path(slug: str, name: str, *, root: str | os.PathLike[str] | None = None) -> Path:
    """Return the checkpoint file path for node ``name`` under ``slug``."""
    if not name:
        raise ValueError("checkpoint_path requires a non-empty node name.")
    return apply_state_dir(slug, root=root) / f"{name}.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` to ``path`` atomically via a same-dir tmp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)  # atomic on POSIX + Windows
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_checkpoint(
    slug: str,
    name: str,
    envelope: dict,
    *,
    root: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically persist a node-result ``envelope`` and return its path."""
    path = checkpoint_path(slug, name, root=root)
    _atomic_write_json(path, envelope)
    return path


def read_checkpoint(
    slug: str,
    name: str,
    *,
    root: str | os.PathLike[str] | None = None,
) -> dict | None:
    """Return the cached envelope iff a ``parse_ok=true`` checkpoint exists.

    A missing file, unreadable/corrupt JSON, or a ``parse_ok=false`` envelope is
    treated as ABSENT (returns ``None``) so the node re-runs on resume.
    """
    path = checkpoint_path(slug, name, root=root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("parse_ok"):
        return None
    return data


__all__ = [
    "STATE_DIRNAME",
    "apply_state_dir",
    "checkpoint_path",
    "write_checkpoint",
    "read_checkpoint",
]
