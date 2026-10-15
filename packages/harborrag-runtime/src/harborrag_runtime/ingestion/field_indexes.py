"""Which ``fields.*`` evidence payload paths each tenant needs indexed.

A source scope declares its filterable facts as facets in ``graph_build.yaml``
(a Jira custom field by display name, say *Skill Set*). Those are exactly the
source fields a caller is expected to filter evidence by -- ``fields.skill_set``
-- so they are the ones given a payload index; without one, each such filter
read every point's on-disk payload and timed out on a large tenant.
"""

from __future__ import annotations

from harborrag_adapters.connectors.jira.content import field_key
from harborrag_core.ingestion import VectorPayload
from harborrag_engine.ingestion.projections.vector import SourceFieldIndex
from harborrag_engine.retrieval.evidence_filters import SOURCE_FIELD_FILTER_PREFIX

from ..config.graph_build import GraphBuildConfig

# A facet may name a standard issue attribute (``status``) rather than a custom
# field. Those live at the top of the payload, not under ``fields``, so indexing
# ``fields.status`` would index a path no point has.
_TOP_LEVEL_PAYLOAD_KEYS = frozenset(VectorPayload.model_fields)


def declared_field_indexes(
    config: GraphBuildConfig,
) -> dict[str, tuple[SourceFieldIndex, ...]]:
    """Per tenant, the source fields its scopes declared as facets.

    The payload key is the connector's normalization of the declared name, so a
    facet must name a custom field by its display name to be indexed under the
    key evidence carries: a ``customfield_NNNNN`` id only becomes the payload key
    when the display name could not be one.
    """

    output: dict[str, tuple[SourceFieldIndex, ...]] = {}
    for tenant in config.tenants:
        paths: dict[str, bool] = {}
        for source in tenant.sources:
            for facet in source.facets:
                key = field_key(facet.field)
                if not key or key in _TOP_LEVEL_PAYLOAD_KEYS:
                    continue
                path = f"{SOURCE_FIELD_FILTER_PREFIX}{key}"
                # The stored value's type comes from the field itself, not from the
                # facet, so one scope declaring it a number is enough to say so.
                paths[path] = paths.get(path, False) or facet.type == "integer"
        if paths:
            output[tenant.tenant_id] = tuple(
                SourceFieldIndex(path=path, numeric=numeric)
                for path, numeric in sorted(paths.items())
            )
    return output


__all__ = ["declared_field_indexes"]
