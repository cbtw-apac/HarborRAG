"""Deprecated names for the ``Connection`` aggregate, kept for one release step."""

from __future__ import annotations

from .connection import Connection, ConnectionStatus

SourceConfig = Connection
SourceStatus = ConnectionStatus

__all__ = ["SourceConfig", "SourceStatus"]
