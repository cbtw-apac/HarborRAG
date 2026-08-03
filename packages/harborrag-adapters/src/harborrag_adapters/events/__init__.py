"""EventBusPort adapters (M2): in-process pub/sub today, Redis later."""

from __future__ import annotations

from .in_process import InProcessEventBus

__all__ = ["InProcessEventBus"]
