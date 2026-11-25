"""ISO 8601 duration parsing for setting values like ``ingestion.activity_timeout``."""

from __future__ import annotations

from harborrag_core.config_store.durations import parse_iso8601_duration_seconds

__all__ = ["parse_iso8601_duration_seconds"]
