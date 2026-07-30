"""Secrets port adapters (M2): dev file-backed today, Vault/KMS in prod later."""

from __future__ import annotations

from .file import FileSecretsRepository

__all__ = ["FileSecretsRepository"]
