"""Compatibility name for the provider-neutral durable ingestion operations."""

from .durable import DurableIngestionOperations as TemporalIngestionOperations

__all__ = ["TemporalIngestionOperations"]
