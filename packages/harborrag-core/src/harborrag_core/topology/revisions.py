"""Code-owned identities for canonical resolution and graph projection contracts."""

from types import MappingProxyType

CONSERVATIVE_RESOLUTION_REVISION = "conservative-v1"

_PROJECTION_REVISION_BY_SCHEMA = MappingProxyType(
    {
        "1": "semantic-v1",
        "2": "semantic-v2",
        "3": "semantic-v3",
        "4": "semantic-v6",
    }
)


def projection_revision_for_schema(schema_version: str) -> str:
    """Resolve one supported extraction schema to its compatible projection."""

    try:
        return _PROJECTION_REVISION_BY_SCHEMA[schema_version]
    except KeyError:
        raise ValueError(f"unsupported topology schema version: {schema_version}") from None
