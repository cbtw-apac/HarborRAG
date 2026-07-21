"""Shared invariants reused by domain dataclass ``__post_init__`` hooks."""

from __future__ import annotations


def require_id(value: str, *, label: str) -> None:
    """Enforce the common domain ID invariant: non-empty, no whitespace."""
    if not value or any(ch.isspace() for ch in value):
        raise ValueError(f"{label} id must be non-empty and contain no whitespace.")
