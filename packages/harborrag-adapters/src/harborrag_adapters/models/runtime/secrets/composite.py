from __future__ import annotations

from collections.abc import Mapping

from ..security import SecretReference, SecretResolver
from .base import parse_secret_reference


class CompositeSecretResolver:
    """Route secret references to explicit provider-specific resolver instances."""

    def __init__(self, resolvers: Mapping[str, SecretResolver]) -> None:
        """Copy resolver mappings without creating hidden global integrations."""

        self._resolvers = {name.lower(): resolver for name, resolver in resolvers.items()}

    def resolve(self, reference: SecretReference) -> str:
        """Resolve one reference through the provider authority in its secret URI."""

        provider = parse_secret_reference(reference).provider
        resolver = self._resolvers.get(provider)
        if resolver is None:
            supported = ", ".join(sorted(self._resolvers)) or "<none>"
            raise KeyError(f"no secret resolver for {provider!r}; configured: {supported}")
        return resolver.resolve(reference)
