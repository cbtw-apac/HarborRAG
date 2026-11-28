"""Validated builder for connector-specific document normalization."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from harborrag_engine.ingestion import (
    BaseDocumentNormalizer,
    DocumentNormalizer,
)

from .router import SourceDocumentNormalizer, source_system_key

SourceNormalizerFactory = Callable[[BaseDocumentNormalizer], BaseDocumentNormalizer]

_SOURCE_SYSTEM_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")


@dataclass(frozen=True, slots=True)
class SourceNormalizerRegistration:
    """Describe one source-owned normalizer without constructing it eagerly.

    Args:
        source_system: Source system key; normalized to lower case on creation.
        factory: Builds the normalizer from the shared fallback normalizer.

    Raises:
        ValueError: ``source_system`` does not start with a letter or has other characters.
        TypeError: ``factory`` is not callable.
    """

    source_system: str
    factory: SourceNormalizerFactory

    def __post_init__(self) -> None:
        """Normalize the source system key and validate the registration."""
        source_system = source_system_key(self.source_system)
        if not _SOURCE_SYSTEM_PATTERN.fullmatch(source_system):
            raise ValueError(
                "source_system must start with a letter and contain only "
                "letters, numbers, underscores, or hyphens"
            )
        if not callable(self.factory):
            raise TypeError("source normalizer factory must be callable")
        object.__setattr__(self, "source_system", source_system)


class SourceDocumentNormalizerBuilder:
    """Compose a normalizer router from independently registered providers.

    Args:
        default_factory: Builds the fallback normalizer shared by every provider.

    Raises:
        TypeError: ``default_factory`` is not callable.
    """

    def __init__(
        self,
        *,
        default_factory: Callable[[], BaseDocumentNormalizer] = DocumentNormalizer,
    ) -> None:
        if not callable(default_factory):
            raise TypeError("default normalizer factory must be callable")
        self._default_factory = default_factory
        self._registrations: dict[str, SourceNormalizerRegistration] = {}

    @property
    def source_systems(self) -> tuple[str, ...]:
        """Return registered providers in deterministic order."""
        return tuple(sorted(self._registrations))

    def register(
        self,
        source_system: str,
        factory: SourceNormalizerFactory,
    ) -> SourceDocumentNormalizerBuilder:
        """Register one provider factory and reject ambiguous ownership.

        Args:
            source_system: Source system the factory serves.
            factory: Builds the normalizer from the shared fallback normalizer.

        Returns:
            This builder, for chaining.
        """
        return self.register_provider(SourceNormalizerRegistration(source_system, factory))

    def register_provider(
        self,
        registration: SourceNormalizerRegistration,
    ) -> SourceDocumentNormalizerBuilder:
        """Register a reusable provider definition.

        Args:
            registration: Provider definition to add.

        Returns:
            This builder, for chaining.

        Raises:
            ValueError: A normalizer is already registered for that source system.
        """
        source_system = registration.source_system
        if source_system in self._registrations:
            raise ValueError(f"normalizer already registered for source_system {source_system!r}")
        self._registrations[source_system] = registration
        return self

    def build(
        self,
        *,
        default: BaseDocumentNormalizer | None = None,
    ) -> SourceDocumentNormalizer:
        """Build an immutable router and share one fallback with all providers.

        Args:
            default: Fallback normalizer; the default factory builds one when omitted.

        Returns:
            Router over every registered provider.

        Raises:
            TypeError: The default factory or a provider factory returns a
                non-normalizer.
        """
        fallback = default or self._default_factory()
        if not isinstance(fallback, BaseDocumentNormalizer):
            raise TypeError("default factory must return BaseDocumentNormalizer")
        providers: dict[str, BaseDocumentNormalizer] = {}
        for source_system, registration in self._registrations.items():
            normalizer = registration.factory(fallback)
            if not isinstance(normalizer, BaseDocumentNormalizer):
                raise TypeError(
                    f"normalizer factory for {source_system!r} must return BaseDocumentNormalizer"
                )
            providers[source_system] = normalizer
        return SourceDocumentNormalizer(default=fallback, providers=providers)
