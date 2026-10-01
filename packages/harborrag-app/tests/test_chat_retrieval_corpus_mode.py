"""Chat retrieval must honour the deployment's corpus access mode.

The agent path (``workflow_control/agent/service.py``) already reads
``runtime.config.runtime.corpus_access_mode`` and puts it on the
``AccessContext`` it searches with, so a tenant that is configured for the
shared corpus gets the relaxed scope everywhere else it asks a question.
Chat built its ``AccessContext`` without ever looking at that setting, so the
same tenant silently fell back to the stricter ``source_acl`` mode the moment
a turn went through chat instead of the agent -- a shared-corpus deployment
with two surfaces, one honouring the setting and one ignoring it.
"""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeRetrievalFacade, FakeRuntime, fake_runtime_config

from harborrag_app.workflow_control.chat.retrieval import search_documents
from harborrag_app.workflow_control.memory.identity import MemoryIdentity
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import MemoryContext

SETTINGS = RuntimeSettings()


def _context(query: str = "What does the release policy say?") -> MemoryContext:
    return MemoryContext(
        messages=(),
        summary=None,
        recalled=(),
        standalone_query=query,
        rewritten=False,
        summary_written=False,
    )


@pytest.mark.asyncio
async def test_search_documents_uses_the_configured_shared_corpus_mode() -> None:
    """A tenant configured as the shared tenant searches in ``tenant_shared`` mode."""

    retrieval = FakeRetrievalFacade()
    runtime = FakeRuntime(
        chat=None,  # type: ignore[arg-type]
        retrieval=retrieval,
        config=fake_runtime_config(
            corpus_access_mode="tenant_shared",
            corpus_shared_tenant_id="ACME",
        ),
    )
    identity = MemoryIdentity(
        tenant_id="ACME",
        principal_id="reader-1",
        user_id="reader-1",
        session_id="session-1",
    )

    await search_documents(
        runtime,  # type: ignore[arg-type]
        _context(),
        identity=identity,
        settings=SETTINGS,
        graph_search=None,
    )

    assert retrieval.request is not None
    assert retrieval.request.access.corpus_mode == "tenant_shared"


@pytest.mark.asyncio
async def test_search_documents_defaults_to_source_acl_when_not_the_shared_tenant() -> None:
    """A tenant other than the configured shared one still gets the strict mode."""

    retrieval = FakeRetrievalFacade()
    runtime = FakeRuntime(
        chat=None,  # type: ignore[arg-type]
        retrieval=retrieval,
        config=fake_runtime_config(
            corpus_access_mode="tenant_shared",
            corpus_shared_tenant_id="OTHER-TENANT",
        ),
    )
    identity = MemoryIdentity(
        tenant_id="ACME",
        principal_id="reader-1",
        user_id="reader-1",
        session_id="session-1",
    )

    await search_documents(
        runtime,  # type: ignore[arg-type]
        _context(),
        identity=identity,
        settings=SETTINGS,
        graph_search=None,
    )

    assert retrieval.request is not None
    assert retrieval.request.access.corpus_mode == "source_acl"
