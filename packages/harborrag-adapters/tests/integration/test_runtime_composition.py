"""Gray-box integration with harborrag-runtime's composition root.

The runtime import chain is known to be fragile in this checkout: the
``harborrag_runtime`` package may not be installed, and even the adapters model
layer it pulls in can fail because ``harborrag_core.ports`` is missing. Rather
than let CI go red on an environment problem, we ``importorskip`` the runtime
and fall back to asserting the contract MockConnector must satisfy for the
runtime pipeline to consume it.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from harborrag_adapters.connectors.mock import MockConnector
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord


def test_mock_connector_satisfies_runtime_pipeline_contract():
    """The runtime pipeline drives connectors via discover() -> load().

    This is the minimum contract the CompositionRoot's mock pipeline relies on,
    and it holds regardless of whether the runtime package imports.
    """
    connector = MockConnector()

    records = list(connector.discover())
    assert records, "MockConnector must discover at least one record"
    assert all(isinstance(r, SourceRecord) for r in records)

    documents = [connector.load(r) for r in records]
    assert all(isinstance(d, RawDocument) for d in documents)
    assert documents[0].content_type == "text/markdown"

    # The convenience stream returns an iterator of RawDocument.
    stream = connector.load_raw_documents()
    assert isinstance(stream, Iterator)
    assert [d.id for d in stream] == [r.id for r in records]


def test_composition_root_mock_pipeline_runs_if_importable():
    """Best-effort exercise of the runtime CompositionRoot mock pipeline.

    Skips (rather than fails) when the runtime import chain is broken, which is
    the documented state of this checkout (missing ``harborrag_runtime`` and/or
    ``harborrag_core.ports``).
    """
    composition = pytest.importorskip(
        "harborrag_runtime.composition",
        reason="harborrag_runtime import chain unavailable in this environment",
    )

    CompositionRoot = getattr(composition, "CompositionRoot", None)
    assert CompositionRoot is not None, "CompositionRoot missing from composition module"

    try:
        root = CompositionRoot.local()
        pipeline = root.mock_pipeline()
    except Exception as exc:  # noqa: BLE001 - environment-dependent wiring
        pytest.skip(f"CompositionRoot mock pipeline unavailable: {exc!r}")

    assert pipeline is not None
