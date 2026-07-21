from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from harborrag_core.models.errors import HarborModelError
from pydantic import BaseModel

from .routing import DeploymentLike


@dataclass(frozen=True, slots=True)
class RoutedAttempt[D: DeploymentLike]:
    """Identify one concrete attempt within the routing state machine."""

    logical_model: str
    deployment: D
    attempt: int


@dataclass(frozen=True, slots=True)
class RoutedResult[T: BaseModel, D: DeploymentLike]:
    """Return a normalized response with explicit transition counters."""

    value: T
    attempt: RoutedAttempt[D]
    retry_count: int
    deployment_failover_count: int
    model_fallback_count: int


class RoutingTransitionType(StrEnum):
    """Distinguish retry and fallback transitions for policy telemetry."""

    RETRY = "retry"
    DEPLOYMENT_FALLBACK = "deployment_fallback"
    MODEL_FALLBACK = "model_fallback"


@dataclass(frozen=True, slots=True)
class RoutingTransition[D: DeploymentLike]:
    """Describe one retry or fallback decision after a provider failure."""

    kind: RoutingTransitionType
    attempt: RoutedAttempt[D]
    error: HarborModelError
    retry_count: int
    deployment_failover_count: int
    model_fallback_count: int
