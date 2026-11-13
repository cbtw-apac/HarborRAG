"""Turn the explorer ``Filter`` into what chat retrieval applies.

Retrieval combines conditions with AND only, so a flat ``and`` of conditions is
supported: ``eq`` and ``in`` on any indexed evidence field, ``gt``/``gte``/``lt``/``lte``
on number fields (``fields.<key>``, never on the exactly matched evidence keys), and
``connection`` (``eq`` or ``in``) to narrow to connections.
Anything else is refused on field ``filters`` rather than ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_engine.retrieval.evidence_filters import (
    EVIDENCE_FILTER_KEYS,
    validate_evidence_filter_keys,
)

from .schemas import Condition, Filter

CONNECTION_FIELD = "connection"
SOURCE_SCOPE_FIELD = "source_scope_id"
_RANGE_OPS = frozenset({"gt", "gte", "lt", "lte"})
_CONNECTION_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*/[a-z0-9][a-z0-9_-]{0,62}$")


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """Evidence filters, and the connections a ``connection`` condition names (``None``: none)."""

    evidence: dict[str, object]
    connections: tuple[str, ...] | None = None


def retrieval_filters(filters: Filter | None) -> RetrievalFilters:
    """Translate a request's ``filters``.

    Args:
        filters: The request's filter, if any.

    Returns:
        The evidence filters and the connections to narrow to.

    Raises:
        HarborValidationError: The filter uses ``or``, nesting, an unsupported operator or
            value, repeats a field, or names a field that is not indexed (field ``filters``).
    """
    if filters is None:
        return RetrievalFilters({})
    if filters.all_of is None:
        raise _refused("or filters are not supported yet; combine conditions with and")
    evidence: dict[str, object] = {}
    connections: tuple[str, ...] | None = None
    for item in filters.all_of:
        if not isinstance(item, Condition):
            raise _refused("nested filters are not supported yet")
        if item.field == CONNECTION_FIELD:
            if connections is not None:
                raise _refused("one connection condition per filter")
            connections = _connection_refs(item)
        else:
            _add(evidence, item)
    try:
        validate_evidence_filter_keys(evidence)
    except HarborValidationError as error:
        raise _refused(str(error)) from error
    return RetrievalFilters(evidence, connections)


def _add(evidence: dict[str, object], condition: Condition) -> None:
    if condition.op in _RANGE_OPS:
        if condition.field in EVIDENCE_FILTER_KEYS:
            raise _refused(f"{condition.field} is matched exactly; ranges apply to number fields")
        bounds = evidence.setdefault(condition.field, {})
        number = condition.value
        if (
            not isinstance(bounds, dict)
            or condition.op in bounds
            or isinstance(number, bool)
            or not isinstance(number, int | float)
        ):
            raise _refused(f"{condition.op} on {condition.field} needs one number")
        bounds[condition.op] = number
        return
    if condition.field in evidence:
        raise _refused(f"one eq or in condition per field: {condition.field}")
    if condition.field == SOURCE_SCOPE_FIELD and not (
        isinstance(condition.value, str) or _strings(condition.value)
    ):
        raise _refused(f"{SOURCE_SCOPE_FIELD} takes source scope ids")
    if condition.op == "eq" and isinstance(condition.value, str | int | float | bool):
        evidence[condition.field] = condition.value
    elif condition.op == "in" and _strings(condition.value):
        evidence[condition.field] = list(condition.value)
    else:
        raise _refused(f"{condition.op} on {condition.field} is not supported")


def _connection_refs(condition: Condition) -> tuple[str, ...]:
    if condition.op == "eq" and isinstance(condition.value, str):
        refs: tuple[str, ...] = (condition.value,)
    elif condition.op == "in" and _strings(condition.value):
        refs = tuple(condition.value)
    else:
        raise _refused("connection takes eq or in with connection refs")
    if not all(_CONNECTION_REF.fullmatch(ref) for ref in refs):
        raise _refused("a connection ref is <project_id>/<connection_id>")
    return refs


def _strings(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) for item in value)


def _refused(message: str) -> HarborValidationError:
    return HarborValidationError(message, {"field": "filters"})
