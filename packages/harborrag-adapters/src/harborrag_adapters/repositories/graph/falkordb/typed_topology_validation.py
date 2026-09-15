"""Validate immutable typed projection manifests before graph materialization."""

from harborrag_core.topology.records import (
    CanonicalAssertion,
    CanonicalMention,
    DocumentTopologyBuild,
)


def validate_typed_build(build: DocumentTopologyBuild, tenant_id: str) -> None:
    if (
        not tenant_id.strip()
        or not build.build_id.strip()
        or not build.document_id.strip()
        or not build.document_version_id.strip()
    ):
        raise ValueError("typed projection requires explicit tenant/build/source version ownership")
    chunks = set(build.chunk_ids)
    if len(chunks) != len(build.chunk_ids) or any(not chunk.strip() for chunk in chunks):
        raise ValueError("typed projection has invalid or duplicate chunk identities")
    records: tuple[CanonicalMention | CanonicalAssertion, ...] = (
        *build.mentions,
        *build.assertions,
    )
    if any(
        record.tenant_id != tenant_id
        or record.build_id != build.build_id
        or record.document_id != build.document_id
        or record.document_version_id != build.document_version_id
        for record in records
    ):
        raise ValueError(
            "typed observation does not belong to the declared tenant/build/document version"
        )
    if any(record.chunk_id not in chunks for record in records):
        raise ValueError("typed observation references unsupported evidence chunk")
    if len({item.mention_id for item in build.mentions}) != len(build.mentions) or len(
        {item.assertion_id for item in build.assertions}
    ) != len(build.assertions):
        raise ValueError("typed projection has duplicate observation identities")
    representation_ids = {item.chunk_id for item in build.representations}
    if len(representation_ids) != len(build.representations) or not representation_ids <= chunks:
        raise ValueError("typed projection has duplicate or foreign representations")
