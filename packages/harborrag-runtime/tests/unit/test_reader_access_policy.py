"""Every serving context is held to the corpus policy and the access file at the facades."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from retrieval_test_support import FakeVectorRepository
from retrieval_test_support import policy as _retrieval_policy
from retrieval_test_support import resources as _retrieval_resources

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborAuthError
from harborrag_core.contracts.reader import (
    DocumentContextRequest,
    DocumentListRequest,
    EntityFindRequest,
    EntityResolveRequest,
    EvidenceFetchRequest,
    EvidenceReadRequest,
    EvidenceReadSelector,
    GraphNodeResolveRequest,
    GraphPathRequest,
    GraphSubgraphRequest,
    GraphTripletRequest,
    RelationSearchRequest,
    RetrievalRequest,
    SemanticPathRequest,
    SourceListRequest,
)
from harborrag_core.retrieval import (
    GraphNodeResolutionQuery,
    GraphNodeSelectorKind,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_core.security import AccessContext
from harborrag_core.topology.search import EvidenceBundle
from harborrag_engine.retrieval import RetrievalLane
from harborrag_engine.security.access_service import CorpusPolicy
from harborrag_runtime.config.access_file import AccessFileWatcher
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.retrieval import RuntimeRetrievalService
from harborrag_runtime.retrieval.access_policy import (
    FileEntitlements,
    ReaderAccessPolicy,
    corpus_policy_from_settings,
)
from harborrag_runtime.retrieval.facades import GraphFacade, KnowledgeFacade, RetrievalFacade

_ACCESS_LOGGER = "harborrag.runtime.config.access_file"
_SHARED = CorpusPolicy(mode="tenant_shared", shared_tenant_id="shared")
_SOURCE_ACL = CorpusPolicy(mode="source_acl", shared_tenant_id=None)
_REFUSAL = "not entitled to any datasource in this tenant"

_CATALOG = {
    "version": 1,
    "connectors": {
        "confluence-eng": {"provider": "local"},
        "jira-cpm": {"provider": "local"},
        "wiki": {"provider": "local"},
    },
}
_SCOPES = {
    "acme": {
        "confluence-eng": frozenset({"scope-conf"}),
        "jira-cpm": frozenset({"scope-jira"}),
        "wiki": frozenset({"scope-wiki-a", "scope-wiki-b"}),
    }
}
_ALICE = {"principal": "alice-sub", "label": "Alice Example <alice@acme.test>"}
_BOB = {"principal": "bob-sub", "datasources": ["jira-cpm"]}


class _Scopes:
    """A ``ConnectionScopeReader`` over fixed scope ids; a connection never ingested is absent."""

    def __init__(self, scopes: dict[str, dict[str, frozenset[str]]]) -> None:
        self._scopes = scopes
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def source_scope_ids_by_connection(
        self, tenant_id: str, connection_ids: Any
    ) -> dict[str, frozenset[str]]:
        self.calls.append((tenant_id, tuple(connection_ids)))
        known = self._scopes.get(tenant_id, {})
        return {name: known[name] for name in connection_ids if name in known}


class _Config:
    """A connector catalog and an access file under ``tmp_path``, as the runtime reads them."""

    def __init__(self, root: Path) -> None:
        self.catalog = root / "connectors.yaml"
        self.access = root / "access.yaml"
        self.catalog.write_text(yaml.safe_dump(_CATALOG), encoding="utf-8")

    def write(
        self,
        members: list[dict[str, Any]],
        *,
        datasources: tuple[str, ...] = ("confluence-eng", "jira-cpm", "wiki"),
    ) -> None:
        document = {
            "version": 1,
            "tenants": {
                "acme": {
                    "projects": {"beta": {"datasources": list(datasources), "members": members}}
                }
            },
        }
        self.access.write_text(yaml.safe_dump(document), encoding="utf-8")

    def bump_mtime(self) -> None:
        later = time.time_ns() + 5_000_000_000
        os.utime(self.access, ns=(later, later))

    def entitlements(self, scopes: _Scopes | None = None) -> FileEntitlements:
        watcher = AccessFileWatcher(self.access, catalog_path=self.catalog)
        return FileEntitlements(watcher, scopes=scopes or _Scopes(_SCOPES))

    def policy(
        self, *, corpus: CorpusPolicy = _SOURCE_ACL, scopes: _Scopes | None = None
    ) -> ReaderAccessPolicy:
        return ReaderAccessPolicy(corpus=corpus, entitlements=self.entitlements(scopes))


@pytest.fixture
def config(tmp_path: Path) -> _Config:
    return _Config(tmp_path)


def _context(principal: str = "alice-sub", tenant: str = "acme", **fields: Any) -> AccessContext:
    return AccessContext(principal_id=principal, tenant_id=tenant, **fields)


async def _refusal(policy: ReaderAccessPolicy, access: AccessContext) -> HarborAuthError:
    with pytest.raises(HarborAuthError) as caught:
        await policy.hold(access)
    assert caught.value.forbidden is True
    return caught.value


@pytest.mark.asyncio
async def test_without_a_file_the_tenant_policy_sets_the_corpus_mode() -> None:
    policy = ReaderAccessPolicy(corpus=_SHARED, entitlements=None)

    shared = await policy.hold(_context(tenant="shared"))
    other = await policy.hold(_context(tenant="acme"))
    claimed = await policy.hold(_context(tenant="acme", corpus_mode="tenant_shared"))

    assert shared.corpus_mode == "tenant_shared"
    assert other.corpus_mode == "source_acl"
    # A context cannot name its own mode: another tenant's context is source_acl whatever it says.
    assert claimed.corpus_mode == "source_acl"
    assert shared.entitled_source_scopes is None
    assert other.entitled_source_scopes is None


@pytest.mark.asyncio
async def test_without_a_file_the_contexts_own_entitlements_are_kept() -> None:
    policy = ReaderAccessPolicy(corpus=_SOURCE_ACL, entitlements=None)
    own = _context(entitled_source_scopes=frozenset({"scope-conf"}), class_ids=frozenset({"c1"}))

    held = await policy.hold(own)

    assert held.entitled_source_scopes == frozenset({"scope-conf"})
    assert held.class_ids == frozenset({"c1"})
    assert held.principal_id == own.principal_id
    assert held.purpose == "serve"


@pytest.mark.asyncio
async def test_an_empty_entitlement_is_refused_even_without_a_file() -> None:
    policy = ReaderAccessPolicy(corpus=_SOURCE_ACL, entitlements=None)

    refused = await _refusal(policy, _context(entitled_source_scopes=frozenset()))

    assert str(refused) == _REFUSAL


@pytest.mark.asyncio
async def test_a_listed_principal_gets_its_connections_scopes(config: _Config) -> None:
    config.write([_ALICE, _BOB])
    scopes = _Scopes(_SCOPES)
    policy = config.policy(scopes=scopes)

    alice = await policy.hold(_context("alice-sub"))
    bob = await policy.hold(_context("bob-sub"))

    # Alice has the whole project; Bob is narrowed to one of its datasources.
    assert alice.entitled_source_scopes == frozenset(
        {"scope-conf", "scope-jira", "scope-wiki-a", "scope-wiki-b"}
    )
    assert bob.entitled_source_scopes == frozenset({"scope-jira"})
    assert scopes.calls[1] == ("acme", ("jira-cpm",))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "tenant"),
    [("mallory-sub", "acme"), ("alice-sub", "globex")],
    ids=["unlisted-principal", "unlisted-tenant"],
)
async def test_an_unlisted_principal_or_tenant_is_refused(
    config: _Config, principal: str, tenant: str
) -> None:
    config.write([_ALICE, _BOB])

    refused = await _refusal(config.policy(), _context(principal, tenant))

    assert str(refused) == _REFUSAL
    assert principal not in str(refused)


@pytest.mark.asyncio
async def test_the_file_refuses_a_shared_tenant_context_too(config: _Config) -> None:
    config.write([_ALICE])
    policy = config.policy(corpus=CorpusPolicy(mode="tenant_shared", shared_tenant_id="globex"))

    refused = await _refusal(policy, _context("alice-sub", "globex"))

    assert str(refused) == _REFUSAL


@pytest.mark.asyncio
async def test_a_context_can_narrow_but_never_widen(config: _Config) -> None:
    config.write([_ALICE, _BOB])
    policy = config.policy()

    narrowed = await policy.hold(
        _context("alice-sub", entitled_source_scopes=frozenset({"scope-conf", "scope-elsewhere"}))
    )
    # Bob's file grant is the jira datasource only; the context asking for conf too gets nothing more.
    bob = await policy.hold(
        _context("bob-sub", entitled_source_scopes=frozenset({"scope-jira", "scope-conf"}))
    )
    unrestricted = await policy.hold(_context("alice-sub", entitled_source_scopes=None))

    assert narrowed.entitled_source_scopes == frozenset({"scope-conf"})
    assert bob.entitled_source_scopes == frozenset({"scope-jira"})
    assert unrestricted.entitled_source_scopes == frozenset(
        {"scope-conf", "scope-jira", "scope-wiki-a", "scope-wiki-b"}
    )
    # Nothing in common with the file leaves nothing, which is refused.
    refused = await _refusal(
        policy, _context("bob-sub", entitled_source_scopes=frozenset({"scope-conf"}))
    )
    assert str(refused) == _REFUSAL


@pytest.mark.asyncio
@pytest.mark.parametrize("with_file", [False, True], ids=["no-file", "file"])
async def test_a_process_context_is_refused(config: _Config, with_file: bool) -> None:
    config.write([{"principal": "harborrag-runtime"}])
    policy = (
        config.policy() if with_file else ReaderAccessPolicy(corpus=_SOURCE_ACL, entitlements=None)
    )

    refused = await _refusal(policy, AccessContext.system("acme"))

    assert "harborrag-runtime" not in str(refused)


@pytest.mark.asyncio
async def test_an_edit_applies_on_the_next_hold(config: _Config) -> None:
    config.write([_ALICE])
    policy = config.policy()
    before = await policy.hold(_context("alice-sub"))
    await _refusal(policy, _context("bob-sub"))

    config.write([{"principal": "alice-sub", "datasources": ["wiki"]}, _BOB])
    config.bump_mtime()

    after = await policy.hold(_context("alice-sub"))
    bob = await policy.hold(_context("bob-sub"))
    assert before.entitled_source_scopes == frozenset(
        {"scope-conf", "scope-jira", "scope-wiki-a", "scope-wiki-b"}
    )
    assert after.entitled_source_scopes == frozenset({"scope-wiki-a", "scope-wiki-b"})
    assert bob.entitled_source_scopes == frozenset({"scope-jira"})


@pytest.mark.asyncio
async def test_an_invalid_edit_refuses_everyone_until_fixed(
    config: _Config, caplog: pytest.LogCaptureFixture
) -> None:
    config.write([_ALICE, _BOB])
    policy = config.policy()
    await policy.hold(_context("alice-sub"))

    config.access.write_text("version: 1\ntenants: [unterminated", encoding="utf-8")
    config.bump_mtime()
    with caplog.at_level(logging.WARNING, logger=_ACCESS_LOGGER):
        await _refusal(policy, _context("alice-sub"))
        await _refusal(policy, _context("bob-sub"))

    (record,) = [item for item in caplog.records if item.name == _ACCESS_LOGGER]
    message = record.getMessage()
    assert type(_last_exception(config)).__name__ in message
    assert str(config.access) in message
    for secret in ("alice", "bob", "Alice Example", "acme.test"):
        assert secret not in message

    config.write([_ALICE, _BOB])
    config.bump_mtime()
    assert (await policy.hold(_context("alice-sub"))).entitled_source_scopes


def _last_exception(config: _Config) -> BaseException:
    """The exception the watcher hit for the broken file, from loading it directly."""

    from harborrag_runtime.config.access_file import load_access_file
    from harborrag_runtime.config.connectors.loader import load_connector_catalog

    try:
        load_access_file(config.access, catalog=load_connector_catalog(config.catalog))
    except Exception as exc:  # noqa: BLE001 - the test wants whichever type loading raises
        return exc
    raise AssertionError("the broken file loaded")


@pytest.mark.asyncio
async def test_a_connection_with_two_scopes_entitles_both(config: _Config) -> None:
    config.write([{"principal": "alice-sub", "datasources": ["wiki"]}])

    held = await config.policy().hold(_context("alice-sub"))

    assert held.entitled_source_scopes == frozenset({"scope-wiki-a", "scope-wiki-b"})


@pytest.mark.asyncio
async def test_connections_never_ingested_leave_the_principal_refused(config: _Config) -> None:
    config.write([_ALICE, _BOB])
    scopes = _Scopes({"acme": {"confluence-eng": frozenset({"scope-conf"})}})
    policy = config.policy(scopes=scopes)

    # Bob's only connection has no scope yet, so nothing is readable, and the answer is a refusal.
    refused = await _refusal(policy, _context("bob-sub"))
    alice = await policy.hold(_context("alice-sub"))

    assert str(refused) == _REFUSAL
    assert alice.entitled_source_scopes == frozenset({"scope-conf"})


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_read_refuses_everyone(config: _Config) -> None:
    # Missing: the access file was never written.
    refused = await _refusal(config.policy(), _context("alice-sub"))

    assert str(refused) == _REFUSAL


@pytest.mark.asyncio
async def test_the_file_is_read_off_the_event_loop(config: _Config) -> None:
    config.write([_ALICE])
    watcher = AccessFileWatcher(config.access, catalog_path=config.catalog)
    threads: list[int] = []
    original = watcher.current

    def spy() -> Any:
        threads.append(threading.get_ident())
        return original()

    watcher.current = spy  # type: ignore[method-assign]
    entitlements = FileEntitlements(watcher, scopes=_Scopes(_SCOPES))

    scopes = await entitlements.entitled_source_scopes("acme", "alice-sub")

    assert scopes
    assert threads
    assert threading.get_ident() not in threads


@pytest.mark.asyncio
async def test_a_slow_file_read_does_not_stall_the_event_loop(config: _Config) -> None:
    config.write([_ALICE])
    watcher = AccessFileWatcher(config.access, catalog_path=config.catalog)
    original = watcher.current

    def slow() -> Any:
        time.sleep(0.3)
        return original()

    watcher.current = slow  # type: ignore[method-assign]
    entitlements = FileEntitlements(watcher, scopes=_Scopes(_SCOPES))
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    await entitlements.entitled_source_scopes("acme", "alice-sub")
    task.cancel()

    assert ticks >= 5


@pytest.mark.asyncio
async def test_a_tenant_with_no_connections_resolves_no_scopes(config: _Config) -> None:
    config.write([_ALICE])
    scopes = _Scopes(_SCOPES)
    entitlements = config.entitlements(scopes)

    assert await entitlements.entitled_source_scopes("globex", "alice-sub") == frozenset()
    assert await entitlements.entitled_source_scopes("acme", "nobody") == frozenset()
    assert scopes.calls == []


# --- the facades hand the service the held context, never the request's own --------------------


@dataclass
class _Empty:
    """A diagnostics payload ``asdict`` can read."""


class _HoldingService:
    """Records every access context a facade passes it; ``hold_access`` returns a distinct copy."""

    def __init__(self) -> None:
        self.held: AccessContext | None = None
        self.received: dict[str, list[AccessContext]] = {}

    async def hold_access(self, access: AccessContext) -> AccessContext:
        self.held = access.model_copy(update={"actor_id": "held-by-policy"})
        return self.held

    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        values = (*args, *kwargs.values())
        found = [value for value in values if isinstance(value, AccessContext)]
        found += [
            value.access
            for value in values
            if isinstance(getattr(value, "access", None), AccessContext)
        ]
        self.received[name] = found

    async def retrieve(self, *args: Any, **kwargs: Any) -> Any:
        self._record("retrieve", args, kwargs)
        return SimpleNamespace(
            request_id="request-1",
            lane=RetrievalLane.HYBRID,
            results=(),
            diagnostics=_Empty(),
            evidence=EvidenceBundle(),
        )

    async def search_graph_triplets(self, *args: Any, **kwargs: Any) -> Any:
        self._record("search_graph_triplets", args, kwargs)
        return SimpleNamespace(triplets=(), diagnostics=_Empty())

    async def search_graph_paths(self, *args: Any, **kwargs: Any) -> Any:
        self._record("search_graph_paths", args, kwargs)
        return SimpleNamespace(paths=(), diagnostics=_Empty())

    async def search_graph_subgraph(self, *args: Any, **kwargs: Any) -> Any:
        self._record("search_graph_subgraph", args, kwargs)
        return SimpleNamespace(graph=SimpleNamespace(nodes=(), relations=()), diagnostics=_Empty())

    async def lookup_entities(self, *args: Any, **kwargs: Any) -> Any:
        self._record("lookup_entities", args, kwargs)
        return ()

    def __getattr__(self, name: str) -> Callable[..., Awaitable[object]]:
        if name.startswith("_"):
            raise AttributeError(name)

        async def method(*args: Any, **kwargs: Any) -> object:
            self._record(name, args, kwargs)
            return object()

        return method


def _requests(access: AccessContext) -> dict[str, Any]:
    return {
        "retrieval": RetrievalRequest(access, "question"),
        "entities": EntityFindRequest(access, "question"),
        "triplets": GraphTripletRequest(access, GraphTripletQuery(predicate=RelationType.LINKS_TO)),
        "paths": GraphPathRequest(access, GraphPathQuery(start_node="a", end_node="b")),
        "subgraph": GraphSubgraphRequest(access, GraphSubgraphQuery(start_node="a")),
        "documents": DocumentListRequest(access),
        "fetch": EvidenceFetchRequest(access, ("chunk-1",)),
        "read": EvidenceReadRequest(access, (EvidenceReadSelector("chunk-1"),)),
        "context": DocumentContextRequest(access, "document-1"),
        "sources": SourceListRequest(access),
        "nodes": GraphNodeResolveRequest(
            access,
            GraphNodeResolutionQuery(selector_kind=GraphNodeSelectorKind.EXACT_TITLE, value="x"),
        ),
        "resolve": EntityResolveRequest(access, "name"),
        "relations": RelationSearchRequest(access, "entity-1"),
        "semantic_paths": SemanticPathRequest(access, "entity-1", "entity-2"),
    }


# facade class -> public method -> (service method it reaches, how to call it)
_CALLS: dict[tuple[type, str], tuple[str, Callable[[Any, AccessContext], Awaitable[object]]]] = {
    (RetrievalFacade, "search"): ("retrieve", lambda f, a: f.search(_requests(a)["retrieval"])),
    (RetrievalFacade, "find_entities"): (
        "find_entities",
        lambda f, a: f.find_entities(_requests(a)["entities"]),
    ),
    (GraphFacade, "search_triplets"): (
        "search_graph_triplets",
        lambda f, a: f.search_triplets(_requests(a)["triplets"]),
    ),
    (GraphFacade, "find_paths"): (
        "search_graph_paths",
        lambda f, a: f.find_paths(_requests(a)["paths"]),
    ),
    (GraphFacade, "expand_subgraph"): (
        "search_graph_subgraph",
        lambda f, a: f.expand_subgraph(_requests(a)["subgraph"]),
    ),
    (KnowledgeFacade, "list_documents"): (
        "list_documents",
        lambda f, a: f.list_documents(_requests(a)["documents"]),
    ),
    (KnowledgeFacade, "fetch_evidence"): (
        "fetch_evidence",
        lambda f, a: f.fetch_evidence(_requests(a)["fetch"]),
    ),
    (KnowledgeFacade, "read_evidence"): (
        "read_evidence",
        lambda f, a: f.read_evidence(_requests(a)["read"]),
    ),
    (KnowledgeFacade, "get_document_context"): (
        "get_document_context",
        lambda f, a: f.get_document_context(_requests(a)["context"]),
    ),
    (KnowledgeFacade, "list_sources"): (
        "list_readable_sources",
        lambda f, a: f.list_sources(_requests(a)["sources"]),
    ),
    (KnowledgeFacade, "resolve_graph_nodes"): (
        "resolve_graph_nodes",
        lambda f, a: f.resolve_graph_nodes(_requests(a)["nodes"]),
    ),
    (KnowledgeFacade, "resolve_entities"): (
        "resolve_entities",
        lambda f, a: f.resolve_entities(_requests(a)["resolve"]),
    ),
    (KnowledgeFacade, "lookup_entities"): (
        "lookup_entities",
        lambda f, a: f.lookup_entities(access=a, entity_ids=("entity-1",)),
    ),
    (KnowledgeFacade, "find_relations"): (
        "find_semantic_relations",
        lambda f, a: f.find_relations(_requests(a)["relations"]),
    ),
    (KnowledgeFacade, "find_paths"): (
        "find_semantic_paths",
        lambda f, a: f.find_paths(_requests(a)["semantic_paths"]),
    ),
}


def _public_methods(facade: type) -> set[tuple[type, str]]:
    return {
        (facade, name)
        for name, member in inspect.getmembers(facade, inspect.isfunction)
        if not name.startswith("_")
    }


def test_the_call_table_covers_every_public_facade_method() -> None:
    """A new facade method fails here until the held-context test below covers it."""

    public = set().union(
        *(_public_methods(f) for f in (RetrievalFacade, GraphFacade, KnowledgeFacade))
    )

    assert set(_CALLS) == public


@pytest.mark.asyncio
@pytest.mark.parametrize(("facade", "method"), sorted(_CALLS, key=lambda k: (k[0].__name__, k[1])))
async def test_every_facade_method_passes_the_held_context(facade: type, method: str) -> None:
    service = _HoldingService()
    owner = SimpleNamespace(_retrieval_service=lambda: _async_value(service))
    original = _context("reader-sub")
    service_method, call = _CALLS[(facade, method)]

    await call(facade(owner), original)

    assert service.held is not None
    received = service.received[service_method]
    assert received, "the service was handed no access context at all"
    assert all(access is service.held for access in received)
    assert all(access is not original for access in received)
    assert received[0].actor_id == "held-by-policy"


async def _async_value(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_a_refused_hold_stops_the_facade_before_the_service_is_reached() -> None:
    class Refusing(_HoldingService):
        async def hold_access(self, access: AccessContext) -> AccessContext:
            raise HarborAuthError(_REFUSAL, forbidden=True)

    service = Refusing()
    owner = SimpleNamespace(_retrieval_service=lambda: _async_value(service))

    with pytest.raises(HarborAuthError):
        await KnowledgeFacade(owner).fetch_evidence(EvidenceFetchRequest(_context(), ("chunk-1",)))
    with pytest.raises(HarborAuthError):
        await RetrievalFacade(owner).search(RetrievalRequest(_context(), "question"))

    assert service.received == {}


# --- the service and the facades together ---------------------------------------------------


def _service(
    access_policy: ReaderAccessPolicy | None = None,
) -> tuple[RuntimeRetrievalService, FakeVectorRepository]:
    vectors = FakeVectorRepository()
    service = RuntimeRetrievalService(
        resources=_retrieval_resources(vectors=vectors),
        policy=_retrieval_policy(),
        access_policy=access_policy,
    )
    return service, vectors


def _owner(service: RuntimeRetrievalService) -> Any:
    return SimpleNamespace(_retrieval_service=lambda: _async_value(service))


@pytest.mark.asyncio
async def test_a_service_built_without_a_policy_is_source_acl_for_every_tenant() -> None:
    service, _ = _service()

    held = await service.hold_access(_context(tenant="shared", corpus_mode="tenant_shared"))

    assert held.corpus_mode == "source_acl"
    assert held.entitled_source_scopes is None
    await _refusal_of(service, AccessContext.system("shared"))


async def _refusal_of(service: RuntimeRetrievalService, access: AccessContext) -> None:
    with pytest.raises(HarborAuthError) as caught:
        await service.hold_access(access)
    assert caught.value.forbidden is True


@pytest.mark.asyncio
async def test_a_service_holds_contexts_to_the_policy_it_was_given() -> None:
    service, _ = _service(ReaderAccessPolicy(corpus=_SHARED, entitlements=None))

    held = await service.hold_access(_context(tenant="shared"))

    assert held.corpus_mode == "tenant_shared"


@pytest.mark.asyncio
async def test_a_search_for_an_unlisted_principal_never_reaches_the_index(
    config: _Config,
) -> None:
    config.write([_ALICE])
    service, vectors = _service(config.policy())
    facade = RetrievalFacade(_owner(service))

    listed = await facade.search(RetrievalRequest(_context("alice-sub"), "release acceptance"))
    with pytest.raises(HarborAuthError) as refused:
        await facade.search(RetrievalRequest(_context("mallory-sub"), "release acceptance"))

    assert listed.results
    assert refused.value.forbidden is True
    assert len(vectors.hybrid_queries) == 1


# --- the policy a deployment builds, and the closed list of callers ----------------------------


def test_corpus_policy_ignores_an_unused_shared_tenant_id() -> None:
    # An id CorpusPolicy would reject if it were used: it is not, in source_acl mode.
    settings = RuntimeSettings(corpus_access_mode="source_acl", corpus_shared_tenant_id="*")

    policy = corpus_policy_from_settings(settings)

    assert policy == CorpusPolicy(mode="source_acl", shared_tenant_id=None)
    assert policy.mode_for("*") == "source_acl"


def test_corpus_policy_names_the_shared_tenant_in_shared_mode() -> None:
    settings = RuntimeSettings(corpus_access_mode="tenant_shared", corpus_shared_tenant_id="acme")

    policy = corpus_policy_from_settings(settings)

    assert policy.mode_for("acme") == "tenant_shared"
    assert policy.mode_for("globex") == "source_acl"


_PACKAGES = Path(__file__).resolve().parents[3]
_ALLOWED_CALLERS = frozenset(
    {
        "harborrag-runtime/src/harborrag_runtime/retrieval/facades.py",
        "harborrag-runtime/src/harborrag_runtime/composition/readers.py",
        "harborrag-runtime/src/harborrag_runtime/sdk/runtime.py",
    }
)
# A use of the owner's service accessor, called or passed along. A definition is not a use, and
# ``connect_retrieval_service`` is another name.
_SERVICE_ACCESSOR = re.compile(r"(?<!\w)_retrieval_service\b")
_DEFINITION = re.compile(r"\bdef\s+_retrieval_service\b")


def _uses_of_the_service_accessor(packages: Path) -> set[str]:
    return {
        path.relative_to(packages).as_posix()
        for path in packages.glob("*/src/**/*.py")
        if any(
            _SERVICE_ACCESSOR.search(line) and not _DEFINITION.search(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }


def test_serving_reaches_the_service_only_through_facades() -> None:
    offenders = _uses_of_the_service_accessor(_PACKAGES) - _ALLOWED_CALLERS

    assert not offenders, (
        "These files reach the retrieval service without the facades, so the context they pass "
        f"is never held to the corpus policy and the access file: {sorted(offenders)}. The list "
        "of callers is closed: facades.py holds every serving context; readers.py only warms "
        "the service up in start(); sdk/runtime.py only hands the accessor to the memory "
        "providers, which read entity ids under a fixed system principal and memory records, "
        "never the corpus. Route new serving code through a facade method."
    )


def test_the_guard_flags_a_new_caller_and_ignores_definitions(tmp_path: Path) -> None:
    source = tmp_path / "harborrag-x" / "src" / "harborrag_x"
    source.mkdir(parents=True)
    (source / "bypass.py").write_text(
        "async def serve(owner):\n    return await owner._retrieval_service()\n", encoding="utf-8"
    )
    (source / "passed_along.py").write_text(
        "provider = owner._retrieval_service\n", encoding="utf-8"
    )
    (source / "innocent.py").write_text(
        "class Owner:\n    async def _retrieval_service(self): ...\n\n"
        "service = await connect_retrieval_service(settings)\n",
        encoding="utf-8",
    )

    assert _uses_of_the_service_accessor(tmp_path) == {
        "harborrag-x/src/harborrag_x/bypass.py",
        "harborrag-x/src/harborrag_x/passed_along.py",
    }
