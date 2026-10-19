"""Versioned navigation cards and authoritative summary bindings (never evidence)."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar, Literal, Self

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.summary_cards import (
    SUMMARY_CARD_MAX_WORDS,
    SUMMARY_DESCRIPTION_MAX_WORDS,
    card_digest,
    has_legacy_attributes,
)
from harborrag_core.summary_cards import SummaryAttribute as SummaryAttribute
from harborrag_core.summary_cards import SummaryCard as SummaryCard
from harborrag_core.summary_cards import SummaryCoverage as SummaryCoverage
from harborrag_core.summary_cards import SummaryView as SummaryView
from harborrag_core.topology.extraction import digest
from harborrag_core.topology.permissions import PermissionDependency

SUMMARY_REVISION = "summary-projection-v1"
SUMMARY_PERMISSION_BLOCKERS = frozenset(
    {
        "SUMMARY_PERMISSION_SNAPSHOT_MISSING",
        "SUMMARY_PERMISSION_SNAPSHOT_UNKNOWN",
        "SUMMARY_PERMISSION_SNAPSHOT_EXPIRED",
        "SUMMARY_PROCESSING_DISALLOWED",
    }
)
SummaryKind = Literal["Structure", "DocumentVersion", "SourceEntity", "DataSource", "Tenant"]
# Card levels whose text never depends on facets: a section or a document card is
# the same whatever the scope wants to filter its entities by.
LEAF_SUMMARY_KINDS: frozenset[str] = frozenset({"Structure", "DocumentVersion"})
# Conditions that pass on their own, with how soon to come back. Not a day
# (permission blockers) or an hour (spend blockers): ingestion settles on the
# scope's own wait window, and an exhausted per-run call allowance loses nothing
# -- every completed reduction is cached -- so the next run simply continues.
SUMMARY_TRANSIENT_BLOCKERS = frozenset({"SUMMARY_INGESTION_ACTIVE", "summary_call_budget"})
SUMMARY_IMMEDIATE_RETRY_BLOCKERS = frozenset({"summary_call_budget"})


class CardWordBudgets(StrictModel):
    """How long a card may be at each level of the aggregation forest.

    A section card is a navigation hint and stays short. A source entity is the
    first level that spans documents -- a Jira issue with its comments and its
    attachments -- so it is the level where a dossier, not a hint, is what a
    reader and a retrieval index actually need. Budgets are model-affecting, so
    they belong inside the policy fingerprint: widening one regenerates the cards
    it applies to and leaves every other level cached.
    """

    structure: int = Field(default=SUMMARY_CARD_MAX_WORDS, ge=20, le=SUMMARY_DESCRIPTION_MAX_WORDS)
    document_version: int = Field(
        default=SUMMARY_CARD_MAX_WORDS, ge=20, le=SUMMARY_DESCRIPTION_MAX_WORDS
    )
    source_entity: int = Field(
        default=SUMMARY_CARD_MAX_WORDS, ge=20, le=SUMMARY_DESCRIPTION_MAX_WORDS
    )
    data_source: int = Field(
        default=SUMMARY_CARD_MAX_WORDS, ge=20, le=SUMMARY_DESCRIPTION_MAX_WORDS
    )
    tenant: int = Field(default=SUMMARY_CARD_MAX_WORDS, ge=20, le=SUMMARY_DESCRIPTION_MAX_WORDS)

    def for_kind(self, kind: SummaryKind) -> int:
        return {
            "Structure": self.structure,
            "DocumentVersion": self.document_version,
            "SourceEntity": self.source_entity,
            "DataSource": self.data_source,
            "Tenant": self.tenant,
        }[kind]


FACET_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class SummaryFacet(StrictModel):
    """One named, filterable fact a source scope wants on every entity card.

    A facet is copied verbatim from a structured field the connector already
    extracted -- exact, free and authoritative, with no model call. The list is
    closed: a card carries the facets its scope declared and no others, which is
    what lets ``skill_set`` mean one thing across every entity.
    """

    name: str = Field(pattern=FACET_NAME_PATTERN)
    # The connector's field, by display name or field id.
    field: str = Field(min_length=1, max_length=256)
    kind: Literal["text", "integer"] = "text"

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_shape(cls, value: Any) -> Any:
        """Read a facet persisted when facets also named where they came from.

        Scope rows keep the policy they were configured with, so a worker has to
        read the old shape at least until ``configure`` rewrites the row.
        """

        if isinstance(value, Mapping) and ("source" in value or "hint" in value):
            return {key: item for key, item in value.items() if key not in {"source", "hint"}}
        return value


class SummaryPolicy(StrictModel):
    revision: str = SUMMARY_REVISION
    processing_policy_revision: str | None = None
    model_fingerprint: str = Field(min_length=1)
    max_fan_in: int = Field(default=8, ge=2, le=32)
    max_input_bytes: int = Field(default=24000, ge=2048, le=30000)
    max_input_tokens: int = Field(default=6000, ge=512)
    max_calls: int = Field(default=64, ge=1, le=10000)
    card_words: CardWordBudgets = Field(default_factory=CardWordBudgets)
    # Per source scope; the same tenant may declare different facets per scope.
    facets: tuple[SummaryFacet, ...] = Field(default=(), max_length=12)
    # How many cards one run generates at a time. Scheduling, not model-affecting.
    max_concurrency: int = Field(default=4, ge=1, le=100)
    debounce_seconds: float = Field(default=5, ge=0, le=300)
    max_wait_seconds: float = Field(default=60, ge=1, le=3600)
    tenant_enabled: bool = False

    @field_validator("facets", mode="before")
    @classmethod
    def drop_model_read_facets(cls, value: Any) -> Any:
        """Forget facets a stored policy asked the model to read.

        That facet source no longer exists. Dropping them lets the stored policy
        parse, and the resulting fingerprint differs from the configured one, so
        ``configure`` replaces the row and the affected cards regenerate.
        """

        if not isinstance(value, list | tuple):
            return value
        return tuple(
            item
            for item in value
            if not (isinstance(item, Mapping) and item.get("source") == "extracted")
        )

    @model_validator(mode="after")
    def distinct_facets(self) -> Self:
        names = [facet.name for facet in self.facets]
        if len(set(names)) != len(names):
            raise ValueError("summary facet names must be distinct")
        return self

    _SCHEDULING_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"debounce_seconds", "max_wait_seconds", "max_calls", "tenant_enabled", "max_concurrency"}
    )

    @property
    def fingerprint(self) -> str:
        # Scheduling and per-run allowance do not affect the generated text.
        return digest(self.model_dump(exclude=set(self._SCHEDULING_FIELDS)))

    @property
    def leaf_fingerprint(self) -> str:
        """The fingerprint a section or document card is generated under.

        Facets shape only entity cards, so a leaf card keyed on the full fingerprint
        would be regenerated -- hundreds of model calls -- every time a scope tuned
        its filter vocabulary. Keying leaves without facets makes that edit cost
        exactly the entity cards it changes.
        """

        return digest(self.model_dump(exclude=set(self._SCHEDULING_FIELDS) | {"facets"}))


class SummaryManifest(StrictModel):
    node_key: str
    kind: SummaryKind
    source_scope_id: str
    input_document_versions: dict[str, str] = Field(default_factory=dict)
    permission_dependencies: tuple[PermissionDependency, ...] = ()
    child_keys: tuple[str, ...] = ()
    child_artifact_hashes: dict[str, str] = Field(default_factory=dict)
    input_chunk_ids: tuple[str, ...] = ()
    # Source items that belong to this node and had no readable published version
    # when the card was generated. Non-empty exactly when coverage is ``partial``.
    missing_document_ids: tuple[str, ...] = ()
    policy_fingerprint: str
    membership_digest: str
    input_digest: str

    @property
    def binding_digest(self) -> str:
        return digest(self.model_dump(mode="json"))


class SummaryBinding(StrictModel):
    manifest: SummaryManifest
    card: SummaryCard
    generation_key: str
    artifact_hash: str
    revision: int = Field(ge=0)
    updated_at: datetime
    coverage_mode: SummaryCoverage = "complete"

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_card(cls, value: Any) -> Any:
        """Verify a pre-upgrade card against its stored hash, then upgrade it.

        The upgraded card hashes differently from the one stored, so the stored
        hash is checked here, against the card exactly as it was written; only a
        card that passes is re-hashed in its current form.
        """

        if not isinstance(value, Mapping) or not has_legacy_attributes(value.get("card")):
            return value
        if value.get("artifact_hash") != card_digest(value["card"]):
            raise ValueError("summary artifact hash mismatch")
        card = SummaryCard.model_validate(value["card"])
        return {**value, "card": card, "artifact_hash": card.artifact_hash}

    @model_validator(mode="after")
    def verify_artifact(self) -> Self:
        if self.artifact_hash != self.card.artifact_hash:
            raise ValueError("summary artifact hash mismatch")
        if (self.coverage_mode == "partial") != bool(self.manifest.missing_document_ids):
            raise ValueError("partial coverage requires the missing documents that caused it")
        return self


class MissingSourceDocument(StrictModel):
    """A source item the connector discovered that has no readable published version.

    Discovery lists an issue's attachments and comments before any of them is
    parsed, so the expected set is known long before the set that succeeded. The
    difference is what separates a summary written over everything from one
    written over what happened to arrive, and ``parent_document_id`` is what lets
    the difference be attributed to the entity it belongs to.
    """

    document_id: str = Field(min_length=1, max_length=128)
    parent_document_id: str | None = Field(default=None, min_length=1, max_length=128)


class SummaryLease(StrictModel):
    tenant_id: str
    source_scope_id: str
    revision: int
    fence: int
    policy: SummaryPolicy
    lease_until: datetime


class SummarySnapshot(StrictModel):
    tenant_id: str
    source_scope_id: str
    document_versions: dict[str, str]
    permission_dependencies: tuple[PermissionDependency, ...]
    membership_digest: str


def generation_key(
    tenant_id: str, policy: SummaryPolicy, payload: object, *, fingerprint: str | None = None
) -> str:
    return digest([tenant_id, fingerprint or policy.fingerprint, payload])
