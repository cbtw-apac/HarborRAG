"""Every expensive route holds a capacity lease with a function-scoped deadline."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from harborrag_app.api.capacity_dependency import hold_api_capacity, require_api_capacity
from harborrag_app.api.routes.legacy_ingestions import router as legacy_ingestions_router
from harborrag_app.api.v1.admin import router as admin_router
from harborrag_app.api.v1.agent import router as agent_router
from harborrag_app.api.v1.chat import router as chat_router
from harborrag_app.api.v1.ingestion import router as ingestion_router
from harborrag_app.api.v1.retrieval import router as retrieval_router


@pytest.mark.parametrize(
    "router",
    [retrieval_router, ingestion_router, admin_router, legacy_ingestions_router],
)
def test_all_expensive_api_routers_require_capacity(router: object) -> None:
    protected_routes = [route for route in router.routes if isinstance(route, APIRoute)]

    assert protected_routes
    for route in protected_routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_api_capacity in dependency_calls, route.path


@pytest.mark.parametrize("router", [chat_router, agent_router])
def test_completion_routes_hold_capacity_across_the_response_but_bound_only_the_handler(
    router: object,
) -> None:
    """The deadline is function-scoped (disarmed before a StreamingResponse body
    is sent) while the capacity lease stays request-scoped (held until the last
    frame). A request-scoped deadline would cancel SSE streams mid-flight."""

    completion_routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute) and route.path.endswith(("/completions", "/resume"))
    ]

    assert completion_routes
    for route in completion_routes:
        deadline = next(
            dependency
            for dependency in route.dependant.dependencies
            if dependency.call is require_api_capacity
        )
        assert deadline.scope == "function", route.path
        lease = next(
            dependency
            for dependency in deadline.dependencies
            if dependency.call is hold_api_capacity
        )
        assert lease.computed_scope == "request", route.path
