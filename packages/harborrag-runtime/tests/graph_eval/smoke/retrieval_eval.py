"""Golden retrieval evaluation of AuthoritativeGraphSearch over an isolated graph.

Seeds the deterministic eval corpus into the FalkorDB graph key
'harborrag-graph-eval' (never the production 'harborrag' graph), controls
document-version visibility with an in-memory ActiveVersionResolver stub, and
asserts exact expectations from golden/.

Usage:
    .venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/retrieval_eval.py

Exit codes: 0 all cases pass, 1 case failure, 2 prerequisites unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# Standalone-script mode: make the tests/ root importable so the shared library
# (graph_eval.corpus, .golden, .health) resolves the same way it does under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graph_eval.corpus import GRAPH_NAME, TENANT_ID, EvalCorpus, build_corpus  # noqa: E402
from graph_eval.eval_metrics import CaseResult, summarize  # noqa: E402
from graph_eval.golden import (  # noqa: E402
    PATH_CASES,
    STALENESS_CASES,
    SUBGRAPH_CASES,
    TRIPLET_CASES,
)
from graph_eval.smoke import configure_logging  # noqa: E402
from graph_eval.smoke.configuration import build_client  # noqa: E402
from harborrag_adapters.repositories.graph.falkordb import (  # noqa: E402
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_core.ingestion import ActiveDocumentVersion  # noqa: E402
from harborrag_core.retrieval import (  # noqa: E402
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId  # noqa: E402
from harborrag_core.schemas.storage import StorageOperationContext  # noqa: E402
from harborrag_engine.retrieval.graph import AuthoritativeGraphSearch  # noqa: E402

logger = logging.getLogger("harborrag.graph_eval.retrieval_eval")


class StaticActiveVersions:
    """ActiveVersionResolver stub: visibility is exactly this mapping."""

    def __init__(self, active: Mapping[str, str]) -> None:
        self._active = dict(active)

    async def active_versions(
        self, document_ids: Sequence[str]
    ) -> dict[str, ActiveDocumentVersion]:
        return {
            document_id: ActiveDocumentVersion(
                document_id=DocumentId(document_id),
                document_version_id=DocumentVersionId(self._active[document_id]),
            )
            for document_id in document_ids
            if document_id in self._active
        }


async def _seed(
    repository: FalkorKnowledgeGraphRepository,
    corpus: EvalCorpus,
    context: StorageOperationContext,
) -> None:
    """Provision, then rewrite this tenant's projection from scratch.

    provision() writes (CREATE INDEX), which is what brings a never-used graph key
    into existence; the tenant delete is write-only Cypher, so the whole seed runs
    before any read and a first run on a fresh key does not hit FalkorDB's
    "Invalid graph operation on empty key". Delete-then-write makes reruns idempotent.
    """

    await repository.provision()
    await repository.delete_tenant_projection(context=context)
    for batch in corpus.batches.values():
        await repository.write_projection(batch.nodes, batch.relations, context=context)


async def _evaluate_cases(
    repository: FalkorKnowledgeGraphRepository,
    corpus: EvalCorpus,
    context: StorageOperationContext,
) -> list[CaseResult]:
    """Ask each golden case's query and score the answer against its expectation."""

    search = AuthoritativeGraphSearch(repository, StaticActiveVersions(corpus.versions))
    results: list[CaseResult] = []
    for path_case in PATH_CASES:
        path_result = await search.paths(
            GraphPathQuery(
                start_node=corpus.source_item_key(path_case.start_doc),
                end_node=corpus.source_item_key(path_case.end_doc),
                relationship_types=path_case.relationship_types,
                max_depth=path_case.max_depth,
            ),
            context=context,
        )
        results.append(path_case.evaluate(path_result, corpus))
    for subgraph_case in SUBGRAPH_CASES:
        subgraph_result = await search.subgraph(
            GraphSubgraphQuery(
                start_node=corpus.source_item_key(subgraph_case.seed_doc),
                max_depth=subgraph_case.max_depth,
                max_nodes=subgraph_case.max_nodes,
            ),
            context=context,
        )
        results.append(subgraph_case.evaluate(subgraph_result, corpus))
    for triplet_case in TRIPLET_CASES:
        triplet_result = await search.triplets(
            GraphTripletQuery(
                subject=corpus.source_item_key(triplet_case.subject_doc),
                predicate=triplet_case.predicate,
            ),
            context=context,
        )
        results.append(triplet_case.evaluate(triplet_result, corpus))
    for staleness_case in STALENESS_CASES:
        # Withdraw the stale documents by pointing their active version elsewhere:
        # every projected node for them then reads as superseded, not as unpublished.
        withdrawn = {
            document_id: (
                f"{version}-superseded" if document_id in staleness_case.stale_docs else version
            )
            for document_id, version in corpus.versions.items()
        }
        stale_search = AuthoritativeGraphSearch(repository, StaticActiveVersions(withdrawn))
        staleness_result = await stale_search.subgraph(
            GraphSubgraphQuery(
                start_node=corpus.source_item_key(staleness_case.seed_doc),
                max_depth=staleness_case.max_depth,
                max_nodes=staleness_case.max_nodes,
            ),
            context=context,
        )
        results.append(staleness_case.evaluate(staleness_result, corpus))
    return results


async def run() -> int:
    corpus = build_corpus()
    try:
        client = build_client(GRAPH_NAME)
        await client.connect()
    except Exception as error:  # noqa: BLE001 - prerequisite probe
        logger.error("prerequisites unavailable: %s", error)
        return 2
    context = StorageOperationContext.system(TENANT_ID, operation_kind="graph-eval")
    # The explicit client= wins, so the config's connection fields are inert; they
    # still have to validate, hence plausible loopback values.
    repository = FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(host="127.0.0.1", port=6379, graph_name=GRAPH_NAME),
        client=client,
    )
    try:
        await _seed(repository, corpus, context)
        results = await _evaluate_cases(repository, corpus, context)
    # Case verdicts are only ever collected into CaseResults, never raised, so anything
    # escaping the block above is infrastructural, not a retrieval verdict.
    except Exception as error:  # noqa: BLE001 - prerequisite probe
        logger.error("prerequisites unavailable: %s", error)
        return 2
    finally:
        await client.close()

    # JSON is for machines: emit it only when stdout is piped/redirected, so an
    # interactive run shows just the per-case lines and verdict.
    if not sys.stdout.isatty():
        print(json.dumps(summarize(results), indent=2, sort_keys=True))
    failures = [result for result in results if not result.passed]
    for result in results:
        if result.passed:
            logger.info("pass: %s", result.name)
        else:
            logger.error("CASE FAILURE %s: %s", result.name, result.detail)
    verdict = logger.error if failures else logger.info
    verdict(
        "%s: %d/%d cases passed",
        "FAIL" if failures else "PASS",
        len(results) - len(failures),
        len(results),
    )
    return 1 if failures else 0


if __name__ == "__main__":
    # No arguments; this exists so --help prints the docstring instead of connecting.
    argparse.ArgumentParser(description=__doc__).parse_args()
    configure_logging()
    raise SystemExit(asyncio.run(run()))
