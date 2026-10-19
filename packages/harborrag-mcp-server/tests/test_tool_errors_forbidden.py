"""A forbidden auth refusal is a 403 with its own message; any other auth error is a 500."""

from __future__ import annotations

import pytest

from harborrag_core.contracts.errors import HarborAuthError
from harborrag_mcp_server.server.tool_errors import public_tool_error


def test_mcp_maps_forbidden_auth_error_to_403() -> None:
    refusal = HarborAuthError("not entitled to any datasource in this tenant", forbidden=True)

    assert public_tool_error(refusal) == ("not entitled to any datasource in this tenant", 403)


@pytest.mark.parametrize("message", ["token expired", "/srv/harborrag/secret-path"])
def test_a_non_forbidden_auth_error_stays_a_500_named_by_its_class(message: str) -> None:
    status = public_tool_error(HarborAuthError(message))

    assert status == ("tool execution failed (HarborAuthError)", 500)
