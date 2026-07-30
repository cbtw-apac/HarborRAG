"""Real connector-to-chunk composition for the chunking smoke check.

Connectors and parsers are built from the same declarative catalogs the
Temporal worker uses (`config/connectors.yaml` and `config/parsers.yaml`,
falling back to their `.example.yaml` templates), and the chunking service is
composed exactly as `build_ingestion_dependencies` composes it: the runtime
`ApproximateTokenCounter` plus the recursive refiner and the optional
markdown/HTML/JSON structure splitters. Chunk shapes are entirely
token-count-driven, so any other counter would report chunking that production
does not perform.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from bootstrap import REPO_ROOT, load_env, load_env_file

from harborrag_adapters.chunking import HarborChunk
from harborrag_adapters.connectors import HarborConnector
from harborrag_adapters.parsers import HarborParserRegistry
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord
from harborrag_engine.ingestion import DocumentNormalizer
from harborrag_engine.ingestion.chunking import (
    ChunkingConfig,
    ChunkingProfile,
    ChunkingRequest,
    ChunkingResult,
    ChunkingRouter,
    ChunkingService,
    build_default_chunking_service,
)
from harborrag_engine.ingestion.indexing import (
    EmbeddedChunk,
    EmbeddingInputPreparer,
    EmbeddingRun,
    IncrementalChunkDiffer,
    IndexingConfig,
    PreparedEmbeddingInput,
)
from harborrag_engine.ingestion.indexing.vector.planner import VectorMutationPlanner
from harborrag_engine.ingestion.indexing.vector.schemas import VectorMutationPlan
from harborrag_runtime.config import load_connector_catalog, load_parser_catalog
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion_dependencies import embedding_dimensions
from harborrag_runtime.tokenization import ApproximateTokenCounter

CONFIG_DIR = REPO_ROOT / "config"
CONNECTOR_ENV_FILES = (Path("env/.env.connector"), Path("env/.env.parser"))
ATTACHMENT_PROVIDERS = frozenset({"confluence", "jira"})
TENANT_ID = "smoke-chunking"
SPLITTER_KINDS = ("markdown", "html", "json")

# A vector point id and its payload are generation-scoped, and a real generation
# id is minted by the ingestion run this check does not perform. Every reported
# point is therefore scoped to one fixed, obviously-synthetic generation.
GENERATION_ID = "smoke-generation"
PLACEHOLDER_MODEL = "unresolved-embedding-model"
PLACEHOLDER_DIMENSIONS = 1


def load_smoke_environment() -> list[str]:
    """Load the connector/parser dotenv files plus the shared smoke dotenv."""

    loaded = [str(path) for path in (load_env(),) if path.is_file()]
    loaded += [
        str(REPO_ROOT / candidate) for candidate in CONNECTOR_ENV_FILES if load_env_file(candidate)
    ]
    return loaded


def catalog_source(filename: str) -> Path:
    """Prefer a real `config/<filename>.yaml`; fall back to its example."""

    real = CONFIG_DIR / f"{filename}.yaml"
    return real if real.exists() else CONFIG_DIR / f"{filename}.example.yaml"


@dataclass(frozen=True, slots=True)
class ChunkingStage:
    """One fully composed connector-to-chunk pipeline for a single connector."""

    connector_name: str
    provider: str
    connector: HarborConnector
    parser: HarborParserRegistry
    normalizer: DocumentNormalizer
    service: ChunkingService
    config: ChunkingConfig
    router: ChunkingRouter
    preparer: EmbeddingInputPreparer
    indexing_config: IndexingConfig
    embedding_identity_source: str
    token_counter_name: str
    available_splitters: tuple[str, ...]

    def profile_for(self, name: str) -> ChunkingProfile:
        """Return the configured profile a chunking result reports by name."""

        return self.config.profiles[name]

    def close(self) -> None:
        """Release connector-owned resources such as local file descriptors."""

        self.connector.close()


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """Every observable artifact one record produced across the four stages."""

    record: SourceRecord
    raw: RawDocument
    parsed: ParsedDocument
    request: ChunkingRequest
    content_category: str
    result: ChunkingResult
    repeated_fingerprint: str
    prepared: tuple[PreparedEmbeddingInput, ...]
    plan: VectorMutationPlan


def _connector_environment(provider: str) -> dict[str, str]:
    """Copy `os.environ` with the documented smoke-only JIRA token alias."""

    values = dict(os.environ)
    if provider == "jira" and not values.get("JIRA_TOKEN") and values.get("JIRA_API_TOKEN"):
        values["JIRA_TOKEN"] = values["JIRA_API_TOKEN"]
    return values


def _indexing_config() -> tuple[IndexingConfig, str]:
    """Build the vector policy the worker builds, and name where it came from.

    Collection, namespace, and every preparation knob are runtime settings, so
    they match production. The embedding model and dimensions decide the
    `embedding_configuration_fingerprint` stored in each payload: they come from
    explicit settings when set, otherwise from the model catalog. That catalog
    expands `HARBOR_EMBED_*` variables, so when embedding is not configured at
    all the identity falls back to a named placeholder rather than failing a
    check that never embeds.
    """

    settings = RuntimeSettings()
    model = settings.embedding_model or PLACEHOLDER_MODEL
    dimensions = settings.embedding_dimensions or PLACEHOLDER_DIMENSIONS
    source = "runtime settings"
    if not (settings.embedding_model and settings.embedding_dimensions):
        try:
            from harborrag_adapters.models.embed import HarborEmbedClientConfig

            catalog = HarborEmbedClientConfig.from_file(settings.model_config_path)
            model = settings.embedding_model or catalog.default_model
            dimensions = settings.embedding_dimensions or embedding_dimensions(catalog, model)
            source = str(settings.model_config_path)
        except Exception:  # noqa: BLE001 - an unconfigured catalog is not a failure here
            model, dimensions, source = PLACEHOLDER_MODEL, PLACEHOLDER_DIMENSIONS, "placeholder"
    config = IndexingConfig(
        embedding_model=model,
        embedding_dimensions=int(dimensions),
        vector_collection=settings.vector_collection,
        graph_namespace=settings.graph_namespace,
    )
    return config, source


def _chunking_service(config: ChunkingConfig) -> tuple[ChunkingService, tuple[str, ...]]:
    """Compose the production chunking service and report its live splitters."""

    token_counter = ApproximateTokenCounter()
    available = tuple(kind for kind in SPLITTER_KINDS if HarborChunk.available(kind))
    splitters = {
        f"{kind}_splitter": (HarborChunk(kind, token_counter) if kind in available else None)
        for kind in SPLITTER_KINDS
    }
    service = build_default_chunking_service(
        config=config,
        token_counter=token_counter,
        refiner=HarborChunk("recursive", token_counter),
        **splitters,
    )
    return service, available


def build_stage(connector_name: str) -> ChunkingStage:
    """Build one configured connector, its parser, and the chunking service.

    Raises:
        ConnectorConfigurationError: If the connector is undefined or a
            referenced environment variable is missing or empty.
        ParserConfigurationError: If the parser catalog cannot be built.
    """

    parser_catalog = load_parser_catalog(catalog_source("parsers"))
    parser = parser_catalog.build_harbor_parser()
    connector_catalog = load_connector_catalog(catalog_source("connectors"))
    definition = connector_catalog.get(connector_name)
    connector = connector_catalog.build(
        connector_name,
        environment=_connector_environment(definition.provider),
        connector_kwargs=(
            {"parser": parser_catalog.build_harbor_parser()}
            if definition.provider in ATTACHMENT_PROVIDERS
            else None
        ),
    )
    config = ChunkingConfig()
    service, available_splitters = _chunking_service(config)
    indexing_config, embedding_identity_source = _indexing_config()
    return ChunkingStage(
        connector_name=connector_name,
        provider=definition.provider,
        connector=connector,
        parser=parser,
        normalizer=DocumentNormalizer(),
        service=service,
        config=config,
        router=ChunkingRouter(config),
        preparer=EmbeddingInputPreparer(ApproximateTokenCounter()),
        indexing_config=indexing_config,
        embedding_identity_source=embedding_identity_source,
        token_counter_name=ApproximateTokenCounter.__name__,
        available_splitters=available_splitters,
    )


def artifact_revision_id(raw: RawDocument) -> str:
    """Derive a content-addressed revision so repeated runs stay comparable."""

    payload = raw.content if isinstance(raw.content, bytes) else raw.content.encode("utf-8")
    return sha256(payload).hexdigest()


def _vector_plan(
    stage: ChunkingStage,
    result: ChunkingResult,
) -> VectorMutationPlan:
    """Plan the vector writes this revision would produce, without embedding.

    The real differ and mutation planner decide the point identities, actions,
    and payloads. No active manifest exists for a probe revision, so every chunk
    classifies as new and plans one UPSERT. Vectors are placeholder zeros purely
    to satisfy the planner's contract; they are never reported or persisted.
    """

    config = stage.indexing_config
    fingerprint = config.embedding_configuration_fingerprint
    diff = IncrementalChunkDiffer().compare(
        result.manifest,
        None,
        target_embedding_configuration_fingerprint=fingerprint,
    )
    placeholder = tuple(0.0 for _ in range(config.embedding_dimensions))
    embeddings = EmbeddingRun(
        chunks=tuple(EmbeddedChunk(record=record, vector=placeholder) for record in result.chunks),
        configuration_fingerprint=fingerprint,
        dimension=config.embedding_dimensions if result.chunks else None,
        embedding_space=config.embedding_model if result.chunks else None,
    )
    return VectorMutationPlanner().plan(
        generation_id=GENERATION_ID,
        tenant_id=result.manifest.tenant_id,
        diff=diff,
        embeddings=embeddings,
        config=config,
    )


def run_record(
    stage: ChunkingStage,
    record: SourceRecord,
    *,
    profile_name: str | None = None,
) -> StageOutcome:
    """Fetch, parse, normalize, and chunk one discovered record twice.

    The second `chunk` call reuses the same normalized document so a changed
    manifest fingerprint reports non-deterministic chunking rather than a
    changed source. It costs no network access and no paid quota.

    The chunks are then run through the indexing stage's own
    `EmbeddingInputPreparer` and vector mutation planner, which render the exact
    text the vector index would embed and the exact points it would upsert.
    Nothing is embedded, and nothing is written to any store.
    """

    raw = stage.connector.load(record)
    parsed = stage.parser.parse(raw)
    document = stage.normalizer.normalize(raw, parsed)
    request = ChunkingRequest(
        tenant_id=TENANT_ID,
        artifact_id=record.id,
        artifact_revision_id=artifact_revision_id(raw),
        document=document,
        source_kind=stage.provider,
        profile_name=profile_name,
    )
    result = stage.service.chunk(request)
    repeated = stage.service.chunk(request)
    return StageOutcome(
        record=record,
        raw=raw,
        parsed=parsed,
        request=request,
        # Whitebox on purpose: the router owns the content-category table, and
        # re-deriving it here would silently drift from the routing decision
        # this report claims to explain.
        content_category=ChunkingRouter._content_category(request),
        result=result,
        repeated_fingerprint=repeated.manifest.fingerprint,
        prepared=stage.preparer.prepare(result.chunks, stage.indexing_config),
        plan=_vector_plan(stage, result),
    )
