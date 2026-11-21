"""The source authorizer opens the control database once and closes it with its owner."""

from __future__ import annotations

import pytest

from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.application.control_plane import source_access
from harborrag_runtime.application.control_plane.source_access import ControlSourceAuthorizer
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_ACCESS = AccessContext(principal_id="svc-demo", tenant_id=TenantId("acme"))


class _Topology:
    def __init__(self) -> None:
        self.calls: list[tuple[str, AccessContext, int]] = []

    async def allowed_source_scope_ids(
        self, tenant_id: str, *, access: AccessContext, limit: int = 10000
    ) -> tuple[str, ...]:
        self.calls.append((tenant_id, access, limit))
        return ("scope-tickets",)


class _Control:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.connects = 0
        self.closes = 0
        self.topology = _Topology()

    async def connect(self) -> None:
        self.connects += 1
        if self.fail:
            raise ConnectionError("control database unreachable")

    async def close(self) -> None:
        self.closes += 1


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[_Control]:
    controls: list[_Control] = []

    def build(settings: RuntimeSettings) -> _Control:
        del settings
        control = _Control(fail=not controls)
        controls.append(control)
        return control

    monkeypatch.setattr(source_access, "build_ingestion_control", build)
    return controls


async def test_authorizer_connects_once_and_closes_on_aclose(built: list[_Control]) -> None:
    authorizer = ControlSourceAuthorizer(RuntimeSettings())

    with pytest.raises(ConnectionError):
        await authorizer.allowed_source_scope_ids("acme", access=_ACCESS)
    first = await authorizer.allowed_source_scope_ids("acme", access=_ACCESS)
    second = await authorizer.allowed_source_scope_ids("acme", access=_ACCESS, limit=5)
    await authorizer.aclose()

    assert first == second == ("scope-tickets",)
    assert [(control.connects, control.closes) for control in built] == [(1, 1), (1, 1)]
    assert built[1].topology.calls == [("acme", _ACCESS, 10000), ("acme", _ACCESS, 5)]


async def test_authorizer_that_never_connected_closes_quietly(built: list[_Control]) -> None:
    await ControlSourceAuthorizer(RuntimeSettings()).aclose()

    assert built == []
