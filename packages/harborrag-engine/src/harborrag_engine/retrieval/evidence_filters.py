"""The filter vocabulary evidence search accepts, derived from what is indexed.

A key without a payload index makes Qdrant scan every point's payload, so the
accepted keys are exactly ``EVIDENCE_PAYLOAD_INDEXES`` plus two prefixed
families. ``fields.<key>`` filters a source item's own typed field and reaches
its attachments, and whether that path is indexed is checked against the live
collection at query time. ``facet.<name>`` filters source-entity summary cards.
The schema and the validation are both rendered from the one list, so the
advertised and the enforced vocabularies cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.indexing import FilterOperator, VectorFilter
from harborrag_engine.ingestion.projections.vector import EVIDENCE_PAYLOAD_INDEXES

SOURCE_FIELD_FILTER_PREFIX = "fields."
FACET_FILTER_PREFIX = "facet."
_SOURCE_FIELD_PATTERN = r"^fields\.[a-z0-9][a-z0-9_]{0,127}$"
"""Key shape a connector writes under ``VectorPayload.fields``."""
_FACET_PATTERN = r"^facet\.[a-z][a-z0-9_]{0,63}$"
"""Facet name shape a source scope declares, as ``FACET_NAME_PATTERN`` allows."""
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
    """Return the ``filters`` input schema of the evidence search tools.

    Every key shares one value shape, which keeps the schema small in each tool
    listing. ``validate_evidence_filter`` enforces the stricter per-key rules,
    such as no range on a keyword key.

    Returns:
        A JSON schema object for the ``filters`` argument.
    """
    return {
        "type": "object",
        "description": (
            "Narrow evidence by indexed payload fields; all conditions must hold. A "
            "string matches exactly and a list matches any member; created_at and "
            "last_modified are exact ISO-8601 strings, not ranges. 'fields.<key>' "
            "filters a source item's own typed field (e.g. fields.skill_set) and reaches its attachments; "
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
    """Reject a filter key that would become an unindexed payload scan.

    Args:
        keys: Filter keys a caller sent.

    Raises:
        HarborValidationError: A key is neither an indexed payload field nor a
            ``fields.<key>`` or ``facet.<name>`` path.
    """
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

    A range on a keyword-indexed key is refused too. The keyword index cannot
    answer it, so it would scan every payload like an unknown key.

    Args:
        filters: The caller's filter, or ``None`` for no filter.

    Raises:
        HarborValidationError: A key is not filterable, or a keyword key has a
            range.
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
