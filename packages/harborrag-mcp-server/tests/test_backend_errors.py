"""Unit tests for backend error classification in tool responses."""

from __future__ import annotations

import pytest

from harborrag_core.models.errors import HarborEmbedConfigurationError
from harborrag_mcp_server.tools.backend_errors import ERROR_CLASSES, classify_backend_error

pytestmark = [pytest.mark.unit]


class AuthenticationError(Exception):
    """Stand-in for a store client's authentication failure."""


def test_authentication_failure_in_cause_chain_is_reported() -> None:
    try:
        try:
            raise AuthenticationError("Authentication required.")
        except AuthenticationError as inner:
            raise RuntimeError("graph store connect failed") from inner
    except RuntimeError as exc:
        result = classify_backend_error(exc, default_component="vector_retrieval")

    assert result == {"error_class": "authentication", "component": "vector_retrieval"}


def test_connection_refused_is_connection() -> None:
    result = classify_backend_error(
        ConnectionRefusedError(61, "refused"), default_component="graph_retrieval"
    )

    assert result["error_class"] == "connection"
    assert result["component"] == "graph_retrieval"


def test_timeout_is_timeout() -> None:
    assert classify_backend_error(TimeoutError(), default_component="x")["error_class"] == "timeout"


def test_embedding_configuration_error_names_the_embedding_component() -> None:
    exc = HarborEmbedConfigurationError("remote provider base URLs must use HTTPS")

    result = classify_backend_error(exc, default_component="vector_retrieval")

    assert result["component"] == "embedding"
    assert result["error_class"] == "configuration"


def test_unknown_error_is_internal_and_leaks_nothing() -> None:
    result = classify_backend_error(
        RuntimeError("secret host 10.0.0.5 password=x"), default_component="vector_retrieval"
    )

    assert result == {"error_class": "internal", "component": "vector_retrieval"}
    assert "10.0.0.5" not in str(result)


def test_error_classes_are_the_documented_set() -> None:
    assert set(ERROR_CLASSES) == {
        "authentication",
        "connection",
        "timeout",
        "configuration",
        "invalid_request",
        "internal",
    }
