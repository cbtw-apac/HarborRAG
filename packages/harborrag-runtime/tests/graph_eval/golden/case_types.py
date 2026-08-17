"""One case type per graph query shape, each scoring its own answer.

A case declares the query in corpus terms -- document ids, not node keys -- and
``evaluate`` resolves those to keys through the corpus before comparing. That is what
keeps ``cases.py`` readable and what makes the expectations gold *by construction*:
a case never restates a node key the projection derived.

Every ``evaluate`` returns a ``CaseResult`` and never raises, so a failed expectation
is a verdict; anything that escapes is infrastructural. ``retrieval_eval.py`` depends
on that split for its exit codes.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.chunking import RelationType
from harborrag_engine.retrieval.graph import (
    AuthoritativePathResult,
    AuthoritativeSubgraphResult,
    AuthoritativeTripletResult,
)

from ..corpus import EvalCorpus
from ..eval_metrics import CaseResult, check


@dataclass(frozen=True, slots=True)
class PathCase:
    name: str
    start_doc: str
    end_doc: str
    max_depth: int
    relationship_types: tuple[RelationType, ...]
    expect_found: bool
    hops: int

    def evaluate(self, result: AuthoritativePathResult, corpus: EvalCorpus) -> CaseResult:
        found = bool(result.paths)
        if found is not self.expect_found:
            return check(
                self.name,
                False,
                f"expected found={self.expect_found}, paths={len(result.paths)}",
            )
        if self.expect_found:
            lengths = {len(path.relations) for path in result.paths}
            if self.hops not in lengths:
                return check(
                    self.name,
                    False,
                    f"no path of {self.hops} hops; got lengths {sorted(lengths)}",
                )
        return check(self.name, True)


@dataclass(frozen=True, slots=True)
class SubgraphCase:
    name: str
    seed_doc: str
    max_depth: int
    max_nodes: int
    expected_docs: frozenset[str]
    forbidden_docs: frozenset[str]

    def evaluate(self, result: AuthoritativeSubgraphResult, corpus: EvalCorpus) -> CaseResult:
        keys = {node.node_key for node in result.graph.nodes}
        missing = {
            document_id
            for document_id in self.expected_docs
            if corpus.source_item_key(document_id) not in keys
        }
        leaked = {
            document_id
            for document_id in self.forbidden_docs
            if corpus.source_item_key(document_id) in keys
        }
        return check(
            self.name,
            not missing and not leaked,
            f"missing={sorted(missing)} leaked={sorted(leaked)}",
        )


@dataclass(frozen=True, slots=True)
class TripletCase:
    name: str
    subject_doc: str
    predicate: RelationType
    expected_object_docs: frozenset[str]

    def evaluate(self, result: AuthoritativeTripletResult, corpus: EvalCorpus) -> CaseResult:
        objects = {triplet.object.node_key for triplet in result.triplets}
        expected = {corpus.source_item_key(d) for d in self.expected_object_docs}
        return check(
            self.name,
            expected <= objects,
            f"expected objects {sorted(self.expected_object_docs)} not all present",
        )


@dataclass(frozen=True, slots=True)
class StalenessCase:
    """Run a subgraph query with some documents' active versions withdrawn."""

    name: str
    seed_doc: str
    stale_docs: frozenset[str]
    max_depth: int
    max_nodes: int
    forbidden_docs: frozenset[str]
    expect_stale_rejections: bool

    def evaluate(self, result: AuthoritativeSubgraphResult, corpus: EvalCorpus) -> CaseResult:
        keys = {node.node_key for node in result.graph.nodes}
        leaked = {
            document_id
            for document_id in self.forbidden_docs
            if corpus.document_version_key(document_id) in keys
            or corpus.chunk_keys(document_id) & keys
        }
        stale_ok = result.diagnostics.stale_count > 0 if self.expect_stale_rejections else True
        return check(
            self.name,
            not leaked and stale_ok,
            f"leaked version-owned nodes={sorted(leaked)} "
            f"stale_count={result.diagnostics.stale_count}",
        )
