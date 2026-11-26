"""Parser catalog: the named parser definitions of ``parsers.yaml`` and how they build.

Definitions here are already validated; reading and checking the YAML belongs to
``parsing`` and ``loader``, and provider constructors to ``providers``. Secrets
and subprocess variables are read from the environment when a parser is built,
never stored in a definition, and ``repr`` hides every setting.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from harborrag_adapters.parsers import HarborParserFactory, HarborParserRegistry
from harborrag_adapters.parsers.common.base import HarborParser, HarborParserEngine
from harborrag_adapters.parsers.common.family import HarborSingleEngineFamilyParser
from harborrag_adapters.parsers.pdf.base import HarborPDFEngine
from harborrag_core.domain.parser import ParsedDocument, ParseInput
from harborrag_runtime.config.errors import ParserConfigurationError
from harborrag_runtime.config.parsers.providers import (
    ParserFactory,
    PdfBackendSpec,
    parser_factory,
    pdf_backend_spec,
)


@dataclass(frozen=True, slots=True)
class PdfBackendDefinition:
    """One PDF backend in an explicitly ordered backend chain.

    Attributes:
        backend: PDF backend name, such as ``liteparse`` or ``docling``.
        settings: Backend constructor options from YAML.
        secret_environment: Backend option name to the environment variable
            that holds its secret value.
        process_environment: Subprocess variable name to the environment
            variable that supplies its value.
    """

    backend: str
    settings: Mapping[str, object] = field(default_factory=dict, repr=False)
    secret_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    process_environment: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Copy backend configuration into read-only mappings."""
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))
        object.__setattr__(
            self,
            "secret_environment",
            MappingProxyType(dict(self.secret_environment)),
        )
        object.__setattr__(
            self,
            "process_environment",
            MappingProxyType(dict(self.process_environment)),
        )

    def build(
        self,
        *,
        parser_name: str,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> HarborPDFEngine:
        """Build the PDF backend with its secrets and subprocess variables resolved.

        Args:
            parser_name: Definition that owns this backend, named in errors.
            environment: Source of referenced variables; ``os.environ`` when None.
            overrides: Settings that take precedence over the YAML ones.

        Returns:
            A new PDF engine.

        Raises:
            ParserConfigurationError: The backend is unsupported, a referenced
                variable is unset or empty, or the backend rejects its settings.
        """
        spec = _pdf_backend_spec_for(self.backend, parser_name=parser_name)
        values = dict(self.settings)
        source_environment = os.environ if environment is None else environment
        for setting, variable_name in self.secret_environment.items():
            values[setting] = _required_environment_value(
                source_environment,
                variable_name,
                parser_name=parser_name,
                backend=self.backend,
            )
        if self.process_environment:
            values["env"] = {
                target: _required_environment_value(
                    source_environment,
                    variable_name,
                    parser_name=parser_name,
                    backend=self.backend,
                )
                for target, variable_name in self.process_environment.items()
            }
        if overrides:
            values.update(overrides)

        try:
            return spec.build(values)
        except (TypeError, ValueError) as exc:
            raise ParserConfigurationError(
                f"Parser {parser_name!r} PDF backend {self.backend!r} is invalid: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class ParserDefinition:
    """One named parser construction recipe loaded from YAML.

    Attributes:
        name: Application-level definition name, the YAML key.
        parser: Stable parser type, such as ``pdf``, ``image`` or ``docx``.
        enabled: Whether the registry uses this definition.
        settings: Parser constructor options from YAML.
        pdf_backends: Ordered PDF backend chain; only ``pdf`` definitions have one.
    """

    name: str
    parser: str
    enabled: bool = True
    settings: Mapping[str, object] = field(default_factory=dict, repr=False)
    pdf_backends: tuple[PdfBackendDefinition, ...] = ()

    def __post_init__(self) -> None:
        """Copy parser settings into a read-only mapping."""
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))

    def build(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, object] | None = None,
        pdf_backend_overrides: Mapping[str, Mapping[str, object]] | None = None,
    ) -> HarborParser:
        """Build the parser after merging YAML settings and code overrides.

        Args:
            environment: Source of referenced variables; ``os.environ`` when None.
            overrides: Settings that take precedence over the YAML ones, applied
                to the parser's own constructor.
            pdf_backend_overrides: Settings applied to each PDF backend's own
                constructor instead, keyed by backend name; backends this does
                not name build from their YAML settings unchanged.

        Returns:
            A new parser family of this definition's parser type.

        Raises:
            ParserConfigurationError: The parser type or a PDF backend is
                unsupported, a referenced variable is unset or empty, or the
                parser rejects its settings.
        """
        factory = _parser_factory_for(self)
        values = dict(self.settings)
        if self.pdf_backends:
            values["backends"] = [
                backend.build(
                    parser_name=self.name,
                    environment=environment,
                    overrides=(
                        pdf_backend_overrides.get(backend.backend)
                        if pdf_backend_overrides
                        else None
                    ),
                )
                for backend in self.pdf_backends
            ]
        if overrides:
            values.update(overrides)

        try:
            return factory(**values)
        except (TypeError, ValueError) as exc:
            raise ParserConfigurationError(
                f"Parser {self.name!r} ({self.parser}) is invalid: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class ParserCatalog:
    """A versioned, immutable collection of named parser definitions.

    Attributes:
        parsers: Definitions by name, in YAML order.
        source_path: File the catalog was loaded from.
        version: Configuration format version of that file.
    """

    parsers: Mapping[str, ParserDefinition]
    source_path: Path
    version: int

    def __post_init__(self) -> None:
        """Copy the parser map into a read-only view."""
        object.__setattr__(self, "parsers", MappingProxyType(dict(self.parsers)))

    def get(self, name: str) -> ParserDefinition:
        """Return one parser definition by application-level name.

        Args:
            name: Definition name, the YAML key.

        Returns:
            The definition, enabled or not.

        Raises:
            ParserConfigurationError: No definition has this name.
        """
        try:
            return self.parsers[name]
        except KeyError as exc:
            raise ParserConfigurationError(f"Unknown configured parser: {name!r}") from exc

    def names(self, *, enabled_only: bool = False) -> list[str]:
        """Return definition names in alphabetical order.

        Args:
            enabled_only: Leave out disabled definitions.

        Returns:
            The definition names.
        """
        return sorted(
            name
            for name, definition in self.parsers.items()
            if definition.enabled or not enabled_only
        )

    def build(
        self,
        name: str,
        *,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> HarborParser:
        """Build one configured parser by application-level name.

        Passes on the ``ParserConfigurationError`` of ``get`` for an unknown
        name and of ``ParserDefinition.build`` for a definition that fails to build.

        Args:
            name: Definition name, the YAML key.
            environment: Source of referenced variables; ``os.environ`` when None.
            overrides: Settings that take precedence over the YAML ones.

        Returns:
            A new parser family of that definition's parser type.
        """
        return self.get(name).build(environment=environment, overrides=overrides)

    def build_enabled(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, HarborParser]:
        """Build every enabled definition, in YAML order.

        Passes on the ``ParserConfigurationError`` of the first enabled
        definition that fails to build.

        Args:
            environment: Source of referenced variables; ``os.environ`` when None.

        Returns:
            New parser families by definition name.
        """
        return {
            name: definition.build(environment=environment)
            for name, definition in self.parsers.items()
            if definition.enabled
        }

    def enabled_pdf_backend(self) -> str | None:
        """Return the backend of the enabled single-engine ``pdf`` definition.

        Returns:
            The backend name, or None when no enabled ``pdf`` definition has
            exactly one backend.
        """
        for definition in self.parsers.values():
            if definition.enabled and _is_single_engine_pdf(definition):
                return definition.pdf_backends[0].backend
        return None

    def pdf_definition_for(self, backend: str) -> str | None:
        """Return the name of a single-engine ``pdf`` definition of ``backend``, enabled or not.

        Args:
            backend: PDF backend name, such as ``docling``.

        Returns:
            The first matching definition name in YAML order, or None.
        """
        for name, definition in self.parsers.items():
            if _is_single_engine_pdf(definition) and definition.pdf_backends[0].backend == backend:
                return name
        return None

    def build_registry(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        pdf_definition: str | None = None,
        max_pages: int | None = None,
        ocr_enabled: bool | None = None,
        ocr_languages: tuple[str, ...] | None = None,
    ) -> HarborParserRegistry:
        """Build the default parser registry with the enabled definitions in place.

        A definition takes over only the formats its engines read: a ``docx``
        definition replaces the DOCX engine and keeps the ``doc``, ``odt`` and
        ``epub`` engines of the document family. A ``pdf`` definition replaces
        the whole PDF family. Families no definition configures keep their
        default engines.

        Args:
            environment: Source of referenced variables; ``os.environ`` when None.
            pdf_definition: Definition, enabled or not, that stands in for the
                enabled ``pdf`` one.
            max_pages: ``capture.max_pdf_pages`` override applied to whichever
                ``pdf`` definition is built; other families are unaffected.
            ocr_enabled: ``parsing.ocr.enabled`` override applied to the image
                OCR engine and to the ``liteparse``/``docling`` PDF backends;
                other PDF backends have no disable switch and are unaffected.
            ocr_languages: ``parsing.ocr.languages`` override applied the same
                way. ``liteparse`` takes the first language, ``docling`` takes
                every language, and the image engine takes them joined with ``+``.

        Returns:
            A new registry of default and configured families.

        Raises:
            ParserConfigurationError: Two enabled definitions configure the same
                family, ``pdf_definition`` names no definition, or a definition
                fails to build.
        """
        image_overrides = _image_ocr_overrides(ocr_enabled=ocr_enabled, ocr_languages=ocr_languages)
        configured: dict[str, tuple[str, HarborParser]] = {}
        for config_name, definition in self._registry_definitions(
            pdf_definition, image_overrides=image_overrides
        ).items():
            overrides: dict[str, object] | None = None
            pdf_backend_overrides: Mapping[str, Mapping[str, object]] | None = None
            if definition.parser == "pdf":
                if max_pages is not None:
                    overrides = {"max_pages": max_pages}
                pdf_backend_overrides = _pdf_ocr_overrides(
                    ocr_enabled=ocr_enabled, ocr_languages=ocr_languages
                )
            elif definition.parser == "image":
                overrides = image_overrides
            parser = definition.build(
                environment=environment,
                overrides=overrides,
                pdf_backend_overrides=pdf_backend_overrides,
            )
            existing = configured.get(parser.parser_name)
            if existing is not None:
                raise ParserConfigurationError(
                    f"Enabled parser definitions {existing[0]!r} and "
                    f"{config_name!r} both configure family {parser.parser_name!r}"
                )
            configured[parser.parser_name] = (config_name, parser)

        parser_families: list[HarborParser] = []
        defaults = HarborParserFactory().create_registry().families()
        for default in defaults:
            replacement = configured.pop(default.parser_name, None)
            parser_families.append(
                default if replacement is None else _merge_family(default, replacement[1])
            )
        parser_families.extend(parser for _, parser in configured.values())
        return HarborParserFactory().create_registry(families=parser_families)

    def ocr_overrides_change_parsing(
        self,
        *,
        ocr_enabled: bool | None,
        ocr_languages: tuple[str, ...] | None,
        pdf_definition: str | None = None,
    ) -> bool:
        """Return whether ``parsing.ocr.*`` overrides change a parser ``build_registry`` builds.

        Compares the overrides with the YAML settings of the same definitions
        ``build_registry`` builds for ``pdf_definition``. A missing on/off switch
        counts as on, the default of every OCR engine here. A missing language
        counts as different, because the engines default to different codes.

        Passes on the ``ParserConfigurationError`` of ``get`` when
        ``pdf_definition`` names no definition.

        Args:
            ocr_enabled: ``parsing.ocr.enabled`` override, or ``None``.
            ocr_languages: ``parsing.ocr.languages`` override, or ``None``.
            pdf_definition: Definition that stands in for the enabled ``pdf`` one.

        Returns:
            True when some built parser would run with a different OCR setting.
        """
        pdf_overrides = (
            _pdf_ocr_overrides(ocr_enabled=ocr_enabled, ocr_languages=ocr_languages) or {}
        )
        image_overrides = (
            _image_ocr_overrides(ocr_enabled=ocr_enabled, ocr_languages=ocr_languages) or {}
        )
        for definition in self._registry_definitions(
            pdf_definition, image_overrides=image_overrides
        ).values():
            if definition.parser == "pdf" and any(
                _overrides_change(
                    backend.settings,
                    pdf_overrides.get(backend.backend, {}),
                    on_switch=_PDF_OCR_FIELDS[backend.backend][0],
                )
                for backend in definition.pdf_backends
                if backend.backend in _PDF_OCR_FIELDS
            ):
                return True
            if definition.parser == "image" and _overrides_change(
                definition.settings, image_overrides, on_switch=_IMAGE_OCR_ON_SWITCH
            ):
                return True
        return False

    def _registry_definitions(
        self,
        pdf_definition: str | None,
        *,
        image_overrides: Mapping[str, object] | None = None,
    ) -> dict[str, ParserDefinition]:
        """Return the enabled definitions, with ``pdf_definition`` in place of the ``pdf`` one.

        With ``image_overrides`` and no enabled ``image`` definition, a settings-free
        ``image`` definition stands in for the default image family, so the overrides
        reach it too.
        """
        definitions = {name: item for name, item in self.parsers.items() if item.enabled}
        if pdf_definition is not None:
            selected = self.get(pdf_definition)
            definitions = {
                name: item for name, item in definitions.items() if item.parser != selected.parser
            }
            definitions[pdf_definition] = selected
        if image_overrides and not any(item.parser == "image" for item in definitions.values()):
            definitions[_DEFAULT_IMAGE_DEFINITION.name] = _DEFAULT_IMAGE_DEFINITION
        return definitions

    def build_harbor_parser(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> HarborParserRegistry:
        """Build the parser registry with the enabled definitions in place.

        Passes on the ``ParserConfigurationError`` of ``build_registry``.

        Args:
            environment: Source of referenced variables; ``os.environ`` when None.

        Returns:
            A new registry, as ``build_registry`` builds it.
        """
        return self.build_registry(environment=environment)


_PDF_OCR_FIELDS: Mapping[str, tuple[str, str]] = {
    "liteparse": ("ocr_enabled", "ocr_language"),
    "docling": ("do_ocr", "ocr_lang"),
}
"""Backend's own enable/language field names; a backend not listed here has no
real disable switch (``paddleocr``, ``mineru``) or does no OCR (``pymupdf``),
so ``parsing.ocr.*`` leaves it unaffected rather than passing it an unsupported
constructor argument."""

_IMAGE_OCR_ON_SWITCH = "enabled"
"""The image OCR engine's own on/off field."""

_DEFAULT_IMAGE_DEFINITION = ParserDefinition(name="default-image", parser="image")
"""The default image family as a definition: the image factory with no settings builds
the same engine the default registry does."""

_UNSET = object()
"""Stands in for a setting the YAML leaves out, so it never equals an override."""


def _pdf_ocr_overrides(
    *, ocr_enabled: bool | None, ocr_languages: tuple[str, ...] | None
) -> dict[str, dict[str, object]] | None:
    """Return each OCR-capable PDF backend's own override, or ``None`` for neither."""
    if ocr_enabled is None and not ocr_languages:
        return None
    overrides: dict[str, dict[str, object]] = {}
    for backend, (enabled_field, languages_field) in _PDF_OCR_FIELDS.items():
        backend_overrides: dict[str, object] = {}
        if ocr_enabled is not None:
            backend_overrides[enabled_field] = ocr_enabled
        if ocr_languages:
            backend_overrides[languages_field] = (
                tuple(ocr_languages) if backend == "docling" else ocr_languages[0]
            )
        overrides[backend] = backend_overrides
    return overrides


def _image_ocr_overrides(
    *, ocr_enabled: bool | None, ocr_languages: tuple[str, ...] | None
) -> dict[str, object] | None:
    """Return the image OCR engine's own override, or ``None`` for neither."""
    if ocr_enabled is None and not ocr_languages:
        return None
    overrides: dict[str, object] = {}
    if ocr_enabled is not None:
        overrides[_IMAGE_OCR_ON_SWITCH] = ocr_enabled
    if ocr_languages:
        overrides["lang"] = "+".join(ocr_languages)
    return overrides


def _overrides_change(
    settings: Mapping[str, object], overrides: Mapping[str, object], *, on_switch: str
) -> bool:
    """Return whether ``overrides`` set a value ``settings`` does not already hold.

    A missing ``on_switch`` counts as on. A list in either mapping compares equal to
    the tuple of its items.
    """
    for key, value in overrides.items():
        current = settings.get(key, True if key == on_switch else _UNSET)
        if _comparable(current) != _comparable(value):
            return True
    return False


def _comparable(value: object) -> object:
    """Return ``value`` with a list turned into a tuple, so YAML lists match tuples."""
    return tuple(value) if isinstance(value, list) else value


def _is_single_engine_pdf(definition: ParserDefinition) -> bool:
    return definition.parser == "pdf" and len(definition.pdf_backends) == 1


def _parser_factory_for(definition: ParserDefinition) -> ParserFactory:
    """Return the definition's parser constructor, checked again for definitions made in code."""
    factory = parser_factory(definition.parser)
    if factory is None:
        raise ParserConfigurationError(
            f"Parser {definition.name!r} uses unsupported parser {definition.parser!r}"
        )
    return factory


def _pdf_backend_spec_for(backend: str, *, parser_name: str) -> PdfBackendSpec:
    """Return the backend's constructor metadata, checked again for definitions made in code."""
    spec = pdf_backend_spec(backend)
    if spec is None:
        raise ParserConfigurationError(
            f"Parser {parser_name!r} uses unsupported PDF backend {backend!r}"
        )
    return spec


def _merge_family(default: HarborParser, configured: HarborParser) -> HarborParser:
    """Return ``configured`` plus the default engines of the formats it does not read.

    Single-engine families are rebuilt from their engines, configured ones
    first; any other family, such as PDF, is replaced whole.
    """
    if not (
        isinstance(default, HarborSingleEngineFamilyParser)
        and isinstance(configured, HarborSingleEngineFamilyParser)
    ):
        return configured
    kept = tuple(
        engine for engine in default.engines if not _routes_any_format_of(configured, engine)
    )
    return type(configured)(configured.engines + kept)


def _routes_any_format_of(
    family: HarborSingleEngineFamilyParser,
    engine: HarborParserEngine[ParseInput, ParsedDocument],
) -> bool:
    """Tell whether ``family`` already routes one of the engine's suffixes or media types."""
    return not (
        family.extensions.isdisjoint(engine.suffixes)
        and family.mime_types.isdisjoint(engine.content_types)
    )


def _required_environment_value(
    environment: Mapping[str, str],
    variable_name: str,
    *,
    parser_name: str,
    backend: str,
) -> str:
    """Return one referenced environment value or raise a contextual error."""
    value = environment.get(variable_name)
    if value is None or value == "":
        raise ParserConfigurationError(
            f"Parser {parser_name!r} PDF backend {backend!r} requires "
            f"environment variable {variable_name!r}"
        )
    return value
