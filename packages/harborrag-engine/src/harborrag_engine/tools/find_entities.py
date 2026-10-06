"""Rank source entities against a question, within the facets a caller names.

Chunk search answers "find the passage". This tool answers "which candidates",
which is a different shape of question: the unit is an entity -- a Jira issue
with its comments and attachments -- and the caller can say which entities
qualify (``facets``) separately from what to look for in them (``query``).
Every match carries the evidence chunk ids the summary authority released for
it, so a follow-up ``fetch_evidence`` cites real passages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from harborrag_core.contracts.errors import HarborCapabilityError, HarborValidationError
from harborrag_core.contracts.reader import ENTITY_FIND_LIMIT, EntityFindRequest
from harborrag_core.contracts.tools import ToolBehavior, ToolInvocationContext, ToolSpec

from .reader_base import ReaderTool
from .reader_support import failure, success
from .retrieval_inputs import (
    TENANT_PROPERTY,
    access,
    integer,
    mapping,
    string_list,
    success_or_failure_schema,
    text,
)

logger = logging.getLogger("harborrag.runtime.tools.entities")

_FACET_VALUE = {
    "oneOf": [
        {"type": "string", "minLength": 1, "maxLength": 200},
        {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 200},
            "minItems": 1,
            "maxItems": 24,
        },
        {
            "type": "object",
            "properties": {
                "gte": {"type": "number"},
                "gt": {"type": "number"},
                "lte": {"type": "number"},
                "lt": {"type": "number"},
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
    ]
}

FIND_ENTITIES_SPEC = ToolSpec(
    "find_entities",
    "Rank source entities (for example Jira issues with their comments and attachments) "
    "against a question, optionally restricted to entities whose declared facets match. "
    "Use for questions about whole entities -- which candidates fit a role, which issues "
    "concern a topic -- rather than for finding one passage. Each match returns the "
    "entity's summary, its facets and the evidence chunk ids released for it; pass those "
    "ids to fetch_evidence to cite. Facet names and types are the ones each source "
    "declares, listed per source by list_sources as entity_facets; a text facet takes a "
    "value or a list of values (matched case-insensitively), an integer facet takes "
    "gte/gt/lte/lt. An empty answer is only conclusive when completion.complete is true. "
    "Reason entity_index_unavailable means no entity index has been built for this tenant "
    "yet, and entities_without_released_evidence means some ranked entities have no "
    "current summary yet (withheld_entity_count says how many): in both cases answer "
    "with vector_search instead of concluding that nothing matches.",
    {
        "type": "object",
        "required": ["tenant_id", "query"],
        "properties": {
            "tenant_id": TENANT_PROPERTY,
            "query": {"type": "string", "minLength": 1, "maxLength": 4000},
            "facets": {
                "type": "object",
                "additionalProperties": _FACET_VALUE,
                "maxProperties": 12,
                "default": {},
            },
            "source_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 100,
                "uniqueItems": True,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": ENTITY_FIND_LIMIT,
                "default": 10,
            },
        },
        "additionalProperties": False,
    },
    output_schema=success_or_failure_schema(
        {
            "type": "object",
            "required": ["ok", "request_id", "matches", "completion"],
            "properties": {
                "ok": {"const": True},
                "request_id": {"type": "string", "minLength": 1},
                "contract_revision": {"type": "string"},
                # Ranked entities left out for want of a released summary; non-zero
                # only alongside the entities_without_released_evidence reason.
                "withheld_entity_count": {"type": "integer", "minimum": 0},
                "matches": {
                    "type": "array",
                    "maxItems": ENTITY_FIND_LIMIT,
                    "items": {
                        "type": "object",
                        "required": [
                            "node_key",
                            "rank",
                            "score",
                            "description",
                            "coverage_mode",
                            "attributes",
                            "evidence_chunk_ids",
                        ],
                        "properties": {
                            "node_key": {"type": "string", "minLength": 1},
                            "source_id": {"type": ["string", "null"]},
                            "rank": {"type": "integer", "minimum": 1},
                            "score": {"type": "number"},
                            "description": {"type": ["string", "null"]},
                            "coverage_mode": {
                                "type": ["string", "null"],
                                "enum": ["complete", "partial", "empty", None],
                            },
                            "attributes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["name", "values"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "values": {"type": "array", "items": {"type": "string"}},
                                        "from_document_ids": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                },
                            },
                            "evidence_chunk_ids": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "completion": {
                    "type": "object",
                    "required": ["complete", "reasons"],
                    "properties": {
                        "complete": {"type": "boolean"},
                        "reasons": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "additionalProperties": False,
        }
    ),
    behavior=ToolBehavior(read_only=True, idempotent=True),
)


@dataclass(slots=True)
class FindEntitiesTool(ReaderTool):
    spec = FIND_ENTITIES_SPEC

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
        context: ToolInvocationContext | None = None,
    ) -> dict[str, object]:
        if context is not None and principal_id != context.access.principal_id:
            raise PermissionError("tool principal does not match invocation context")
        try:
            request = EntityFindRequest(
                access=access(arguments, principal_id),
                query=text(arguments, "query"),
                facets=mapping(arguments, "facets"),
                source_scope_ids=string_list(arguments, "source_ids"),
                limit=integer(arguments, "limit", 10, minimum=1, maximum=ENTITY_FIND_LIMIT),
            )
            response = await self.require_retrieval().find_entities(request)
            # Truncation is one reason among several: a missing index or hits with
            # no released summary leave the answer short too, and an empty list
            # must not read as "nothing matched" when it is "could not look".
            reasons = [*response.reasons, *(["candidate_limit"] if response.truncated else [])]
            return success(
                response.request_id,
                {
                    "matches": [
                        {
                            "node_key": match.node_key,
                            "source_id": match.source_scope_id,
                            "rank": match.rank,
                            "score": match.score,
                            "description": match.description,
                            "coverage_mode": match.coverage_mode,
                            "attributes": list(match.attributes),
                            "evidence_chunk_ids": list(match.evidence_chunk_ids),
                        }
                        for match in response.matches
                    ],
                    "withheld_entity_count": response.withheld_count,
                },
                complete=not reasons,
                reasons=reasons,
            )
        except (HarborValidationError, ValueError) as exc:
            return failure(str(exc))
        except HarborCapabilityError as exc:
            return failure(str(exc))
        except Exception:
            logger.exception("find_entities failed")
            return failure("entity search failed")


__all__ = ["FIND_ENTITIES_SPEC", "FindEntitiesTool"]
