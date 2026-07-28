from __future__ import annotations

import pytest

from harborrag_adapters.repositories.graph.traversal import GraphTraversalSyntax


def test_arrows_for_every_supported_direction() -> None:
    assert GraphTraversalSyntax.arrows("outgoing") == ("-", "->")
    assert GraphTraversalSyntax.arrows("incoming") == ("<-", "-")
    assert GraphTraversalSyntax.arrows("both") == ("-", "-")


def test_arrows_rejects_unsupported_direction() -> None:
    with pytest.raises(ValueError, match="unsupported graph direction"):
        GraphTraversalSyntax.arrows("sideways")
