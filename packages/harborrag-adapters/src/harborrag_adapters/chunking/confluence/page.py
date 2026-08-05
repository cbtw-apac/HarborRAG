from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_core.chunking import encoded_identifier
from harborrag_core.domain import CanonicalDocument, DocumentProvenance

from .adf import AdfDocumentParser
from .block_models import ConfluenceBuildContext
from .errors import ConfluenceNormalizationError, UnsupportedConfluenceBodyError
from .hierarchy import ConfluenceHierarchyBuilder
from .macros import ConfluenceMacroHandlerRegistry
from .markup import ConfluenceMarkupParser
from .nodes import ConfluenceNode
from .schemas import ConfluencePageInput


class ConfluencePageNormalizer:
    """Normalize one Confluence page into the existing canonical Document contract."""

    def __init__(
        self,
        macro_registry: ConfluenceMacroHandlerRegistry | None = None,
    ) -> None:
        self._adf = AdfDocumentParser()
        self._markup = ConfluenceMarkupParser()
        self._macros = macro_registry or ConfluenceMacroHandlerRegistry()

    def normalize(self, page: ConfluencePageInput) -> CanonicalDocument:
        """Prefer ADF, then storage format, then rendered HTML with explicit warnings."""

        root, representation, parser_warnings = self._parse_body(page)
        document_id = page.document_id or f"confluence://{page.space_key}/{page.page_id}"
        document_version_id = page.document_version_id or encoded_identifier(
            "document-version",
            {
                "document_id": document_id,
                "source_version": page.page_version,
            },
        )
        result = ConfluenceHierarchyBuilder(
            ConfluenceBuildContext(
                document_id=document_id,
                document_version_id=document_version_id,
                source_version=page.page_version,
                source_url=page.source_url,
                title=page.title,
            ),
            macro_registry=self._macros,
        ).build(root)
        warnings = tuple(dict.fromkeys((*parser_warnings, *result.warnings)))
        return CanonicalDocument(
            id=document_id,
            title=page.title,
            content=list(result.elements),
            content_type="confluence_page",
            provenance=DocumentProvenance(
                source="confluence",
                record_id=page.page_id,
                url=page.source_url,
                permissions=dict(page.permissions),
                tags=list(page.labels),
                extra={
                    "page_id": page.page_id,
                    "page_version": page.page_version,
                    "document_version_id": document_version_id,
                    "space_id": page.space_id,
                    "space_key": page.space_key,
                    "ancestor_ids": tuple(identifier for identifier, _ in page.ancestors),
                    "ancestor_titles": tuple(title for _, title in page.ancestors),
                    "labels": page.labels,
                    "body_representation": representation,
                    "parser_warnings": warnings,
                },
            ),
            relations=list(result.relations),
            raw=None,
            blocks=result.blocks,
            table_artifacts=result.tables,
            body_representation=representation,
            warnings=warnings,
        )

    def normalize_payload(
        self,
        payload: Mapping[str, Any],
        *,
        source_url: str,
        default_space_key: str = "",
    ) -> CanonicalDocument:
        """Normalize a connector payload without retaining the provider object."""

        return self.normalize(
            ConfluencePageInput.from_api_payload(
                payload,
                source_url=source_url,
                default_space_key=default_space_key,
            )
        )

    def _parse_body(
        self,
        page: ConfluencePageInput,
    ) -> tuple[ConfluenceNode, str, tuple[str, ...]]:
        warnings: list[str] = []
        available = False
        if page.adf is not None and (not isinstance(page.adf, str) or page.adf.strip()):
            available = True
            try:
                return self._adf.parse(page.adf), "adf", tuple(warnings)
            except ConfluenceNormalizationError as exc:
                warnings.append(f"adf parsing failed; tried next representation: {exc}")
        for representation, body in (
            ("storage", page.storage),
            ("rendered_html", page.rendered_html),
        ):
            if body is None or not body.strip():
                continue
            available = True
            try:
                return self._markup.parse(body), representation, tuple(warnings)
            except ConfluenceNormalizationError as exc:
                warnings.append(
                    f"{representation} parsing failed; tried next representation: {exc}"
                )
        if available:
            raise UnsupportedConfluenceBodyError(
                f"Confluence page {page.page_id!r} has no parseable body representation"
            )
        raise UnsupportedConfluenceBodyError(
            f"Confluence page {page.page_id!r} has no supported body representation"
        )
