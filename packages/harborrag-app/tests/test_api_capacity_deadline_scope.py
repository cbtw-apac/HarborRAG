from __future__ import annotations

import pytest
from fastapi.dependencies.models import Dependant

from harborrag_app.api.app import create_fastapi_app
from harborrag_app.api.auth.access import api_routes
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.settings import ApiSettings


def _deadline_scopes(dependant: Dependant) -> list[str | None]:
    found = [dependant.scope] if dependant.call is require_api_capacity else []
    for child in dependant.dependencies:
        found.extend(_deadline_scopes(child))
    return found


@pytest.mark.unit
def test_every_capacity_deadline_is_disarmed_when_its_handler_returns() -> None:
    app = create_fastapi_app(
        ApiSettings(env="dev", host="127.0.0.1", auth_mode="none", docs_enabled=False)
    )

    scopes = {
        f"{sorted(route.methods)[0]} {route.path}": _deadline_scopes(route.dependant)
        for route in api_routes(app.routes)
    }
    bounded = {route: found for route, found in scopes.items() if found}

    assert {"GET /v1/admin/failures", "POST /v1/admin/failures/retry"} <= set(bounded)
    assert {route: found for route, found in bounded.items() if found != ["function"]} == {}
