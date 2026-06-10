"""ATS adapter registry for the jobsmith sourcing pipeline (feat-5531c54b)."""

from .base import ATSSourceAdapter, Role, SourceFetchError

__all__ = ["ATSSourceAdapter", "Role", "SourceFetchError"]
