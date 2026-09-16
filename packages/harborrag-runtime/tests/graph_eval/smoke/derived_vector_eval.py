"""Live Qdrant derived-vector write, corruption detection, and repair experiment.

The experiment uses an isolated collection prefix, publishes one frozen contextual
point, deletes it, repairs it from the artifact, corrupts its payload, repairs it again,
and always deletes the temporary collection. Exit: 0 pass, 1 check failure, 2 unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graph_eval.smoke import configure_logging  # noqa: E402
from graph_eval.smoke.configuration import ROOT  # noqa: E402
from harborrag_adapters.repositories.object_store import (  # noqa: E402
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    MemoryObjectStore,
)
from harborrag_adapters.repositories.vector.qdrant.config import (  # noqa: E402
    QdrantDeployment,
    QdrantVectorConfig,
)
from harborrag_adapters.repositories.vector.qdrant.repository import (  # noqa: E402
    QdrantVectorRepository,
)
from harborrag_core.indexing import VectorIndexRecord  # noqa: E402
from harborrag_core.storage import StorageOperationContext  # noqa: E402
from harborrag_core.topology import DocumentTopologyBuild  # noqa: E402
from harborrag_core.topology.derived import (  # noqa: E402
    ContextualIndexProfile,
    ContextualManifest,
)
from harborrag_engine.topology.vector_values import canonical_dense_vector  # noqa: E402
from harborrag_runtime.topology.derived_projection import (  # noqa: E402
    DerivedVectorProjection,
    records_match_at_storage_precision,
)

logger = logging.getLogger("harborrag.graph_eval.derived_vector_eval")
CONTEXT = StorageOperationContext.system("topology-vector-eval")
PROFILE = ContextualIndexProfile(
    model="deterministic-eval", dimension=2, deployment_revision="fixture-v1"
)
POINT_ID = str(UUID(int=1))
COLLECTION_PREFIX = "hr_topology_eval_"


async def _fixture(
    store: MemoryObjectStore,
) -> tuple[VectorIndexRecord, ContextualManifest, DocumentTopologyBuild]:
    vector = list(canonical_dense_vector((0.6, 0.8)))
    record = VectorIndexRecord(
        id=POINT_ID,
        tenant_id=CONTEXT.tenant_id,
        vector=vector,
        payload={
            "record_kind": "contextual",
            "chunk_id": "chunk-1",
            "chunk_ids": ["chunk-1"],
            "cited_chunk_ids": ["chunk-1"],
            "parent_key": "document-1",
            "level": "document",
            "document_id": "document-1",
            "document_version_id": "version-1",
            "build_id": "build-1",
            "projection_point_id": POINT_ID,
            "embedding_profile": PROFILE.fingerprint,
        },
    )
    payload = json.dumps([record.model_dump(mode="json")]).encode()
    artifact = await ImmutableArtifactWriter(store).put(
        ImmutableArtifact(
            bucket="graph-eval",
            key="topology/contextual/build-1.json",
            payload=payload,
            media_type="application/json",
            artifact_kind="contextual-vectors",
        ),
        context=CONTEXT,
    )
    manifest = ContextualManifest(
        artifact=artifact,
        embedding_profile=PROFILE.fingerprint,
        dimension=PROFILE.dimension,
        point_ids=(POINT_ID,),
        index_name=PROFILE.index_name,
    )
    build = DocumentTopologyBuild(
        build_id="build-1",
        job_id="job-1",
        artifact=artifact,
        document_id="document-1",
        document_version_id="version-1",
        chunk_ids=("chunk-1",),
    )
    return record, manifest, build


async def run() -> int:
    load_dotenv(ROOT / "env/.env.database", override=False)
    port = os.getenv("QDRANT_HTTP_PORT", "6333").strip() or "6333"
    vectors = QdrantVectorRepository(
        QdrantVectorConfig(
            deployment=QdrantDeployment.REMOTE,
            url=f"http://127.0.0.1:{port}",
            prefer_grpc=False,
            collection_prefix=COLLECTION_PREFIX,
        )
    )
    try:
        await vectors.connect()
        async with MemoryObjectStore() as store:
            expected, manifest, build = await _fixture(store)
            projection = DerivedVectorProjection(vectors, ImmutableArtifactReader(store))
            await vectors.delete_index(manifest.index_name, context=CONTEXT)
            try:
                await projection.publish(build, manifest, context=CONTEXT)
                actual = await vectors.get_records(
                    manifest.index_name, manifest.point_ids, context=CONTEXT
                )
                initial = records_match_at_storage_precision(actual, [expected])
                await vectors.delete_records(
                    manifest.index_name, manifest.point_ids, context=CONTEXT
                )
                missing_detected = not await vectors.get_records(
                    manifest.index_name, manifest.point_ids, context=CONTEXT
                )
                await projection.publish(build, manifest, context=CONTEXT)
                corrupt = expected.model_copy(
                    update={"payload": {**expected.payload, "build_id": "corrupt"}}
                )
                await vectors.upsert_records(manifest.index_name, [corrupt], context=CONTEXT)
                corrupt_actual = await vectors.get_records(
                    manifest.index_name, manifest.point_ids, context=CONTEXT
                )
                corruption_detected = not records_match_at_storage_precision(
                    corrupt_actual, [expected]
                )
                await projection.publish(build, manifest, context=CONTEXT)
                repaired = records_match_at_storage_precision(
                    await vectors.get_records(
                        manifest.index_name, manifest.point_ids, context=CONTEXT
                    ),
                    [expected],
                )
                checks = {
                    "initial_verify": initial,
                    "missing_point_detected": missing_detected,
                    "payload_corruption_detected": corruption_detected,
                    "repair_verified": repaired,
                }
                print(json.dumps({"checks": checks, "index": manifest.index_name}))
                return 0 if all(checks.values()) else 1
            finally:
                await vectors.delete_index(manifest.index_name, context=CONTEXT)
    except Exception as error:  # noqa: BLE001 - prerequisite probe
        logger.error("prerequisites unavailable: %s", error)
        return 2
    finally:
        await vectors.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    configure_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    raise SystemExit(asyncio.run(run()))
