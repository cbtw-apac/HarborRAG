"""The filter vocabulary evidence search accepts, derived from what is indexed.

``vector_search`` used to accept any object as ``filters``. A key the collection
has no payload index for is not an error to Qdrant -- it is a scan of every
point's on-disk payload, which on a few hundred thousand points ran into the
request deadline instead of failing. The accepted keys are therefore exactly the
provisioned payload indexes (``EVIDENCE_PAYLOAD_INDEXES``), plus two prefixed
families resolved elsewhere:

- ``fields.<key>``: a source item's own typed fields (a Jira custom field such as
  ``fields.skill_set``), traced from the item to its attachments. Only fields a
  source scope declares as a facet are indexed; whether one is, is checked
  against the live collection at query time.
- ``facet.<name>``: a declared facet on source-entity summary cards.

The schema and the validation below are both rendered from that one list so the
advertised vocabulary and the enforced one cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.indexing import FilterOperator, VectorFilter
from harborrag_engine.ingestion.projections.vector import EVIDENCE_PAYLOAD_INDEXES

SOURCE_FIELD_FILTER_PREFIX = "fields."
FACET_FILTER_PREFIX = "facet."
# The normalized key shapes a connector writes under ``fields`` and a scope may
# name a facet with (``VectorPayload.fields`` and ``FACET_NAME_PATTERN``).
_SOURCE_FIELD_PATTERN = r"^fields\.[a-z0-9][a-z0-9_]{0,127}$"
_FACET_PATTERN = r"^facet\.[a-z][a-z0-9_]{0,63}$"
_PREFIXED = (re.compile(_SOURCE_FIELD_PATTERN), re.compile(_FACET_PATTERN))
EVIDENCE_FILTER_KEYS = frozenset(EVIDENCE_PAYLOAD_INDEXES)
_RANGE_OPERATORS = frozenset(
    {
        FilterOperator.GREATER_THAN,
        FilterOperator.GREATER_THAN_OR_EQUAL,
        FilterOperator.LESS_THAN,
        FilterOperator.LESS_THAN_OR_EQUAL,
    }
)

_TEXT = {"type": "string", "minLength": 1}
_RANGE_VALUE: dict[str, object] = {
    "type": "object",
    "properties": {bound: {"type": "number"} for bound in ("gte", "gt", "lte", "lt")},
    "minProperties": 1,
    "additionalProperties": False,
}
# One value shape for every key. Repeating a per-key schema for each of the ~27
# indexed keys made ``filters`` alone about 2k tokens of every tool listing; the
# stricter per-key rules (no ranges on keyword keys) are enforced by
# ``validate_evidence_filter`` either way.
_VALUE: dict[str, object] = {
    "anyOf": [
        _TEXT,
        {"type": "number"},
        {"type": "boolean"},
        {"type": "array", "items": _TEXT, "minItems": 1, "uniqueItems": True},
        _RANGE_VALUE,
    ],
}


def evidence_filters_schema() -> dict[str, object]:
    """The ``filters`` input schema: the indexed keys once, and one value shape."""

    return {
        "type": "object",
        "description": (
            "Narrow evidence by indexed payload fields; all conditions must hold. A "
            "string matches exactly and a list matches any member; created_at/updated_at "
            "are exact ISO-8601 strings, not ranges. 'fields.<key>' filters a source "
            "item's own typed field (e.g. fields.skill_set) and reaches its attachments; "
            "only fields a source scope declares as a facet (see list_sources) are "
            'indexed, and number-typed ones also take a range such as {"gte": 5}. '
            "'facet.<name>' filters source-entity cards. Other keys are rejected."
        ),
        "propertyNames": {
            "anyOf": [
                {"enum": list(EVIDENCE_PAYLOAD_INDEXES)},
                {"pattern": _SOURCE_FIELD_PATTERN},
                {"pattern": _FACET_PATTERN},
            ]
        },
        "additionalProperties": _VALUE,
        "default": {},
    }


def validate_evidence_filter_keys(keys: Iterable[str]) -> None:
    """Reject a filter key that would become an unindexed payload scan."""

    unknown = sorted(
        key
        for key in set(keys)
        if key not in EVIDENCE_FILTER_KEYS and not any(p.fullmatch(key) for p in _PREFIXED)
    )
    if unknown:
        raise HarborValidationError(
            f"unsupported evidence filter key(s): {', '.join(unknown)}; supported keys are "
            f"{', '.join(EVIDENCE_PAYLOAD_INDEXES)}, fields.<key> and facet.<name>",
            details={"unsupported": unknown, "supported": list(EVIDENCE_PAYLOAD_INDEXES)},
        )


def validate_evidence_filter(filters: VectorFilter | None) -> None:
    """Validate a caller's filter before any permission conditions are added.

    A range on one of the keyword-indexed keys is refused too: the keyword index
    cannot answer it, so it would be the same full scan as an unknown key.
    """

    if filters is None:
        return
    conditions = [*filters.must, *filters.should, *filters.must_not]
    validate_evidence_filter_keys(condition.field for condition in conditions)
    ranged = sorted(
        {
            condition.field
            for condition in conditions
            if condition.operator in _RANGE_OPERATORS and condition.field in EVIDENCE_FILTER_KEYS
        }
    )
    if ranged:
        raise HarborValidationError(
            f"evidence filter key(s) {', '.join(ranged)} are exact-match only; "
            "pass a string or a list of strings",
            details={"exact_match_only": ranged},
        )


__all__ = [
    "EVIDENCE_FILTER_KEYS",
    "FACET_FILTER_PREFIX",
    "SOURCE_FIELD_FILTER_PREFIX",
    "evidence_filters_schema",
    "validate_evidence_filter",
    "validate_evidence_filter_keys",
]
