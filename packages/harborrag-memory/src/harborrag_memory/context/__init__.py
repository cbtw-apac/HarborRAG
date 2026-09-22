"""Per-turn conversation-memory context assembly.

``MemoryContextBuilder`` is the single entry point: given the authenticated
``MemoryOwner`` and the question, it returns the verbatim replay window, the
rolling session summary, and the standalone retrieval query as one immutable
``MemoryContext``. ``MemoryPolicy`` is the only knob set.

``MemoryRecall`` and ``MemoryExtractor`` are the long-term halves of the same
loop: recall reads the stored memories worth spending prompt budget on, and
extraction writes back the durable facts a finished turn stated.

Recalled memories are *entity-anchored*: extraction resolves the mentions a
fact is about onto curated knowledge-graph node ids through the core
``MemoryEntityResolver`` port, and recall boosts the memories whose ids
overlap the entities a turn's document retrieval surfaced. Memory is never
written into the document knowledge graph -- only linked to it by id.
"""

from harborrag_core.ports.memory import MemoryEntityResolver, ResolvedEntity

from .builder import MemoryContextBuilder
from .condenser import CondenseResult
from .entities import recalled_entity_ids
from .extraction import MemoryExtractor
from .facts import ExtractedFact
from .hashing import content_hash, normalize_content
from .policy import DEFAULT_RECALL_SCOPES, MemoryPolicy
from .prompts import (
    CONDENSE_SYSTEM_PROMPT,
    CONDENSE_TYPED_SYSTEM_PROMPT,
    CONDENSE_USER_TEMPLATE,
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_TEMPLATE,
    SUMMARY_SYSTEM_PROMPT,
    SUMMARY_USER_TEMPLATE,
)
from .ranking import (
    NO_AFFINITY,
    EntityAnchor,
    ScoredMemory,
    ScoringContext,
    TypeAffinity,
    entity_weight,
    score_memory,
    select,
    type_weight,
)
from .recall import RECALL_MEMORY_TYPES, MemoryRecall
from .result import MemoryContext
from .scoping import scope_query_owner
from .summarizer import LAST_COVERED_KEY, summary_memory_id
from .tokens import approximate_tokens
from .trimming import TokenCounter, total_tokens, trim_window

__all__ = [
    "CONDENSE_SYSTEM_PROMPT",
    "CONDENSE_TYPED_SYSTEM_PROMPT",
    "CONDENSE_USER_TEMPLATE",
    "DEFAULT_RECALL_SCOPES",
    "EXTRACTION_SYSTEM_PROMPT",
    "EXTRACTION_USER_TEMPLATE",
    "LAST_COVERED_KEY",
    "NO_AFFINITY",
    "RECALL_MEMORY_TYPES",
    "SUMMARY_SYSTEM_PROMPT",
    "SUMMARY_USER_TEMPLATE",
    "CondenseResult",
    "EntityAnchor",
    "ExtractedFact",
    "MemoryContext",
    "MemoryContextBuilder",
    "MemoryEntityResolver",
    "MemoryExtractor",
    "MemoryPolicy",
    "MemoryRecall",
    "ResolvedEntity",
    "ScoredMemory",
    "ScoringContext",
    "TokenCounter",
    "TypeAffinity",
    "approximate_tokens",
    "content_hash",
    "entity_weight",
    "normalize_content",
    "recalled_entity_ids",
    "scope_query_owner",
    "score_memory",
    "select",
    "summary_memory_id",
    "total_tokens",
    "trim_window",
    "type_weight",
]
