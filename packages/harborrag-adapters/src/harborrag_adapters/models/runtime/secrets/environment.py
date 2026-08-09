from __future__ import annotations

import os
from collections.abc import Mapping

from ..security import SecretReference
from .base import parse_secret_reference


class EnvironmentSecretResolver:
    """Resolve `secret://env/NAME` references from an explicit environment mapping."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        """Use an injected mapping or the current process environment."""

        self._environment = environment if environment is not None else os.environ

    def resolve(self, reference: SecretReference) -> str:
        """Return one environment value or fail without exposing its name in repr output."""

        parsed = parse_secret_reference(reference)
        if parsed.provider != "env" or len(parsed.segments) != 1 or parsed.field:
            raise ValueError("environment secret URI must be secret://env/NAME")
        name = parsed.segments[0]
        try:
            return self._environment[name]
        except KeyError as exc:
            raise KeyError("environment secret is not configured") from exc
