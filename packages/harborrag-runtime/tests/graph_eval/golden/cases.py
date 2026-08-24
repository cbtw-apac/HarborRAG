"""The golden cases themselves: every expectation the live retrieval eval asserts.

Each case names corpus documents and the answer the deterministic build guarantees --
see ``case_types.py`` for how a case scores its answer, and ``corpus.py`` for the
topology those answers are read off. ``test_corpus.py`` fails any case naming a
document the corpus no longer has, which is CI's only reach into this file.
"""

from __future__ import annotations

from harborrag_core.chunking import RelationType

from .case_types import PathCase, StalenessCase, SubgraphCase, TripletCase

PATH_CASES: tuple[PathCase, ...] = (
    PathCase(
        name="two-hop links_to chain runbook->decisions",
        start_doc="runbook",
        end_doc="decisions",
        max_depth=4,
        relationship_types=(RelationType.LINKS_TO,),
        expect_found=True,
        hops=2,
    ),
    PathCase(
        name="hop bound excludes two-hop chain at depth 1",
        start_doc="runbook",
        end_doc="decisions",
        max_depth=1,
        relationship_types=(RelationType.LINKS_TO,),
        expect_found=False,
        hops=2,
    ),
    # Mixed predicates over the Jira set: HR-1 --blocks--> HR-2 --links_to--> HR-5. Only
    # RELATES_TO joins HR-5 to HR-1 directly, so restricting the types to these two is
    # what forces the two-hop answer.
    PathCase(
        name="mixed links_to/blocks chain HR-1->HR-5",
        start_doc="HR-1",
        end_doc="HR-5",
        max_depth=3,
        relationship_types=(RelationType.LINKS_TO, RelationType.BLOCKS),
        expect_found=True,
        hops=2,
    ),
    # Two files under docs/guides. Their shared directory node is what joins them, so this
    # case only passes if source-entity identity deduplicates that directory across the two
    # documents' batches -- the property every provider's contains spine depends on.
    PathCase(
        name="github files join through their shared directory",
        start_doc="setup-guide",
        end_doc="deploy-guide",
        max_depth=2,
        relationship_types=(RelationType.CONTAINS,),
        expect_found=True,
        hops=2,
    ),
    PathCase(
        name="hop bound excludes the shared-directory join at depth 1",
        start_doc="setup-guide",
        end_doc="deploy-guide",
        max_depth=1,
        relationship_types=(RelationType.CONTAINS,),
        expect_found=False,
        hops=2,
    ),
    PathCase(
        name="sharepoint items join through their shared folder",
        start_doc="security-policy",
        end_doc="retention-schedule",
        max_depth=2,
        relationship_types=(RelationType.CONTAINS,),
        expect_found=True,
        hops=2,
    ),
    # security-policy sits directly in .../Policies/Security and deep-audit two folders
    # below it. Every folder is keyed by its drive-relative path, so that shared folder is
    # one node and the two items meet through it: file -> Security -> Audits -> 2026 ->
    # file. Keying only the *last* folder of a path by the item's parent id made the same
    # folder two nodes and forced this walk up to the drive and back down in six hops.
    PathCase(
        name="sharepoint same-folder subtree meets through one shared folder",
        start_doc="security-policy",
        end_doc="deep-audit",
        max_depth=4,
        relationship_types=(RelationType.CONTAINS,),
        expect_found=True,
        hops=4,
    ),
)

SUBGRAPH_CASES: tuple[SubgraphCase, ...] = (
    SubgraphCase(
        name="architecture 1-hop neighbors over source links",
        seed_doc="architecture",
        max_depth=1,
        max_nodes=50,
        expected_docs=frozenset({"runbook", "decisions", "incident"}),
        forbidden_docs=frozenset(),
    ),
    # The Confluence page tree: team-handbook is one hop from its parent page, while the
    # attachment hangs off team-handbook and so stays outside a depth-1 expansion.
    SubgraphCase(
        name="space-overview 1-hop hierarchy neighbor",
        seed_doc="space-overview",
        max_depth=1,
        max_nodes=50,
        expected_docs=frozenset({"team-handbook"}),
        forbidden_docs=frozenset({"handbook-pdf"}),
    ),
    # deep-config lives under src/, and legacy-notes hangs straight off the repository, so
    # both are further than the shared docs/guides directory that reaches deploy-guide.
    SubgraphCase(
        name="github 2-hop expansion reaches only the sibling file",
        seed_doc="setup-guide",
        max_depth=2,
        max_nodes=60,
        expected_docs=frozenset({"deploy-guide"}),
        forbidden_docs=frozenset({"deep-config", "legacy-notes"}),
    ),
    SubgraphCase(
        name="github 1-hop expansion stops at the directory",
        seed_doc="setup-guide",
        max_depth=1,
        max_nodes=60,
        expected_docs=frozenset(),
        forbidden_docs=frozenset({"deploy-guide"}),
    ),
    # drive-readme sits at the drive root and deep-audit four folders down a diverging
    # chain; only retention-schedule shares security-policy's leaf folder.
    SubgraphCase(
        name="sharepoint 2-hop expansion reaches only the same-folder item",
        seed_doc="security-policy",
        max_depth=2,
        max_nodes=60,
        expected_docs=frozenset({"retention-schedule"}),
        forbidden_docs=frozenset({"drive-readme", "deep-audit"}),
    ),
)

TRIPLET_CASES: tuple[TripletCase, ...] = (
    TripletCase(
        name="runbook links_to architecture triplet",
        subject_doc="runbook",
        predicate=RelationType.LINKS_TO,
        expected_object_docs=frozenset({"architecture"}),
    ),
    # HR-2 carries the canonical `blocks`, HR-3 carries the inverted `is_blocked_by`;
    # both must answer with HR-1 as the subject, or the reversal flipped an edge.
    TripletCase(
        name="HR-1 blocks HR-2 and HR-3 triplets",
        subject_doc="HR-1",
        predicate=RelationType.BLOCKS,
        expected_object_docs=frozenset({"HR-2", "HR-3"}),
    ),
)

STALENESS_CASES: tuple[StalenessCase, ...] = (
    StalenessCase(
        name="stale architecture version is filtered with diagnostics",
        seed_doc="runbook",
        stale_docs=frozenset({"architecture"}),
        max_depth=3,
        max_nodes=80,
        forbidden_docs=frozenset({"architecture"}),
        expect_stale_rejections=True,
    ),
)
