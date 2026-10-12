from __future__ import annotations

from dataclasses import dataclass
from re import compile

from pydantic import ValidationError

from harborrag_core.chunking import (
    ChunkKind,
    ChunkQuality,
    ChunkRecord,
    ChunkRelation,
    ChunkSecurity,
    ConnectorType,
    DocumentKind,
    RecordKind,
    RelationType,
)
from harborrag_core.contracts.chunking import ChunkContextualizer, TokenCounter
from harborrag_core.schemas.ids import ChunkId, DocumentId, DocumentVersionId, TenantId

from ..config import ChunkingProfile
from ..errors import ChunkValidationError
from ..identity import ChunkIdentity, ChunkIdentityBuilder
from ..schemas import ChunkCandidate, ChunkingRequest
from .context import ChunkContextBuilder
from .contextualizer import HEADER_SEPARATOR, DefaultChunkContextualizer, header_context

_ROLE_TOKEN_PATTERN = compile(r"[._-]+")
_CHUNK_KIND_RULES = (
    (frozenset({"table"}), ChunkKind.TABLE),
    (frozenset({"code"}), ChunkKind.CODE),
    (frozenset({"comment"}), ChunkKind.COMMENT),
    (frozenset({"event"}), ChunkKind.EVENT),
    (frozenset({"jira", "field"}), ChunkKind.JIRA_FIELD),
)
_DOCUMENT_KIND_BY_CONNECTOR = {
    ConnectorType.CONFLUENCE: DocumentKind.CONFLUENCE_PAGE,
    ConnectorType.JIRA: DocumentKind.JIRA_ISSUE,
    ConnectorType.LOCAL: DocumentKind.LOCAL_FILE,
}


@dataclass(frozen=True, slots=True)
class CanonicalChunkInput:
    """Validated engine values required to build one canonical chunk."""

    request: ChunkingRequest
    candidate: ChunkCandidate
    identity: ChunkIdentity
    content_hash: str
    ordinal: int
    previous: ChunkIdentity | None
    next_: ChunkIdentity | None
    strategy_name: str
    strategy_version: str
    profile: ChunkingProfile
    configuration_hash: str
    contextualize_embeddings: bool


class CanonicalChunkFactory:
    """Translate one identity-bearing candidate into a canonical record."""

    def __init__(
        self,
        token_counter: TokenCounter,
        identity_builder: ChunkIdentityBuilder,
        contextualizer: ChunkContextualizer | None = None,
    ) -> None:
        self._token_counter = token_counter
        self._identity = identity_builder
        self._context = ChunkContextBuilder(identity_builder)
        self._contextualizer = contextualizer or DefaultChunkContextualizer(token_counter)

    def build(self, values: CanonicalChunkInput) -> ChunkRecord:
        """Build one complete immutable chunk without mutating its candidate."""

        try:
            return self._build_record(values)
        except ValidationError as error:
            if any(details["loc"][:1] == ("metadata",) for details in error.errors()):
                raise ChunkValidationError("chunk metadata is not JSON serializable") from error
            raise ChunkValidationError("canonical chunk validation failed") from error

    def _build_record(self, values: CanonicalChunkInput) -> ChunkRecord:
        request = values.request
        candidate = values.candidate
        identity = values.identity
        citation_locator = self._context.citation_locator(request, candidate)
        hierarchy = self._context.hierarchy(
            request,
            candidate,
            identity,
            values.previous,
            values.next_,
        )
        record_kind = self.record_kind_for_role(candidate.role)
        search_text = self._search_text(candidate)
        embedding_text = candidate.content
        context_truncated = False
        if values.contextualize_embeddings and record_kind == RecordKind.EVIDENCE:
            embedding_text, context_truncated = self._embedding_text(values)
        return ChunkRecord(
            strategy_version=values.strategy_version,
            chunk_id=ChunkId(identity.chunk_id),
            logical_chunk_id=ChunkId(identity.logical_chunk_id),
            content_hash=values.content_hash,
            connector_type=self._connector_type(request),
            document_kind=self._document_kind(request, candidate.role),
            record_kind=record_kind,
            chunk_kind=self.kind_for_role(candidate.role),
            tenant_id=TenantId(request.tenant_id),
            connection_id=self._source_value(
                request,
                "connection_id",
                request.connector_type,
            ),
            source_scope_id=self._source_value(
                request,
                "source_scope_id",
                request.tenant_id,
            ),
            source_item_id=self._source_value(
                request,
                "source_item_id",
                request.document.id,
            ),
            source_version=self._source_value(
                request,
                "source_version",
                request.document_version_id,
            ),
            document_id=DocumentId(request.document.id),
            document_version_id=DocumentVersionId(request.document_version_id),
            ordinal=values.ordinal,
            content=candidate.content,
            embedding_text=embedding_text,
            search_text=search_text,
            token_count=self._token_counter.count(candidate.content),
            citation_locator=citation_locator,
            hierarchy=hierarchy,
            security=ChunkSecurity(
                permission_set_id=self._identity.permission_set_id(
                    tenant_id=request.tenant_id,
                    permissions=request.document.provenance.permissions,
                )
            ),
            relations=self._relations(request),
            quality=ChunkQuality(),
            table_locator=self._context.table_locator(
                request=request,
                candidate=candidate,
                content_hash=values.content_hash,
                is_table=self.kind_for_role(candidate.role) == ChunkKind.TABLE,
            ),
            metadata=self._metadata(values, context_truncated=context_truncated),
        )

    @staticmethod
    def kind_for_role(role: str) -> ChunkKind:
        """Map existing structural strategy roles to stable chunk kinds."""

        tokens = CanonicalChunkFactory._role_tokens(role)
        return next(
            (
                chunk_kind
                for required_tokens, chunk_kind in _CHUNK_KIND_RULES
                if required_tokens <= tokens
            ),
            ChunkKind.TEXT,
        )

    @staticmethod
    def record_kind_for_role(role: str) -> RecordKind:
        return (
            RecordKind.ROUTE
            if "route" in CanonicalChunkFactory._role_tokens(role)
            else RecordKind.EVIDENCE
        )

    @staticmethod
    def _role_tokens(role: str) -> frozenset[str]:
        return frozenset(filter(None, _ROLE_TOKEN_PATTERN.split(role.strip().lower())))

    @staticmethod
    def _connector_type(request: ChunkingRequest) -> ConnectorType:
        supplied = request.document.provenance.extra.get("connector_type")
        value = (
            supplied if isinstance(supplied, str) and supplied.strip() else request.connector_type
        )
        return ConnectorType(value.strip().casefold())

    @classmethod
    def _document_kind(
        cls,
        request: ChunkingRequest,
        role: str,
    ) -> DocumentKind:
        metadata = request.document.provenance.extra
        supplied = metadata.get("document_kind")
        if isinstance(supplied, str):
            return DocumentKind(supplied.strip().casefold())
        binding = metadata.get("binding_kind")
        if (
            isinstance(binding, str) and binding.strip().upper() == "ATTACHMENT"
        ) or "attachment" in cls._role_tokens(role):
            return DocumentKind.ATTACHMENT
        connector = cls._connector_type(request)
        return _DOCUMENT_KIND_BY_CONNECTOR.get(
            connector,
            DocumentKind(f"{connector.value}_document"),
        )

    @staticmethod
    def _source_value(
        request: ChunkingRequest,
        key: str,
        fallback: str,
    ) -> str:
        value = request.document.provenance.extra.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        return fallback

    @staticmethod
    def _search_text(candidate: ChunkCandidate) -> str:
        if CanonicalChunkFactory.record_kind_for_role(candidate.role) == RecordKind.ROUTE:
            return candidate.content
        local_context = candidate.structural_path[-1:] or ()
        symbol = candidate.metadata.get("symbol")
        symbols = (symbol,) if isinstance(symbol, str) and symbol.strip() else ()
        scope = candidate.metadata.get("code_scope")
        scopes = tuple(
            line.strip()
            for line in (scope.splitlines() if isinstance(scope, str) else ())
            if line.strip()
        )
        return "\n".join((*local_context, *symbols, *scopes, candidate.content))

    @staticmethod
    def _relations(request: ChunkingRequest) -> tuple[ChunkRelation, ...]:
        supported = {relation.value: relation for relation in RelationType}
        # A chunk relation is keyed by type and target, so two source links that
        # differ only in provenance -- e.g. two separate Jira "relates to" links to
        # the same issue -- collapse to one. ChunkRecord rejects duplicates.
        unique: dict[tuple[str, str], ChunkRelation] = {}
        for relation in request.document.relations:
            key = (relation.predicate, relation.target_id)
            if relation.predicate in supported and key not in unique:
                unique[key] = ChunkRelation(
                    relation_type=supported[relation.predicate],
                    target_id=relation.target_id,
                )
        return tuple(unique.values())

    def _embedding_text(self, values: CanonicalChunkInput) -> tuple[str, bool]:
        """Render content behind its context header within the hard token limit.

        Returns the text and whether any context had to be left out to fit.
        """

        candidate = values.candidate
        context = header_context(
            values.request,
            role=candidate.role,
            structural_path=candidate.structural_path,
            metadata=candidate.metadata,
        )
        full = self._contextualizer.header(context)
        if not full:
            return candidate.content, False
        budget = (
            values.profile.maximum_tokens
            - self._token_counter.count(candidate.content)
            - self._token_counter.count(HEADER_SEPARATOR)
        )
        header = self._contextualizer.header(context, maximum_tokens=budget) if budget > 0 else ""
        text = self._contextualizer.render(header, candidate.content)
        if header and self._token_counter.count(text) > values.profile.maximum_tokens:
            header, text = "", candidate.content
        return text, header != full

    @classmethod
    def _metadata(
        cls,
        values: CanonicalChunkInput,
        *,
        context_truncated: bool = False,
    ) -> dict[str, object]:
        # An attachment is its own document, and most strategies copy nothing from
        # provenance onto its chunks. Its parent's item id is what links a CV back
        # to the issue whose fields describe it, so every chunk carries it.
        parent = values.request.document.provenance.extra.get("parent_source_item_id")
        return {
            **({"parent_source_item_id": str(parent)} if parent else {}),
            **values.candidate.metadata,
            "source_version": cls._source_value(
                values.request,
                "source_version",
                values.request.document_version_id,
            ),
            "boundary_kind": values.candidate.boundary_kind.value,
            "forced_split": values.candidate.forced_split,
            "structural_anchor": values.candidate.anchor,
            "local_part_index": values.candidate.local_part_index,
            "chunker_name": values.strategy_name,
            "chunker_version": values.strategy_version,
            "profile_name": values.profile.name,
            "configuration_hash": values.configuration_hash,
            **({"context_truncated": True} if context_truncated else {}),
        }
