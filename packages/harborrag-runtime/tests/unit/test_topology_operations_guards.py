"""Guard paths of the topology operator entry points: workers, rebuilds and inspection."""

from contextlib import asynccontextmanager
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from harborrag_core.topology import ExtractionProfile, TopologyPolicy
from harborrag_core.topology.records import TopologyBuildContent
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.topology import operations


def _connect(value):
    @asynccontextmanager
    async def connect(*_args, **_kwargs):
        yield value

    return connect


def _profile():
    return ExtractionProfile(
        model="model", deployment_revision="deployment", prompt_digest="prompt"
    )


class _StopWatching(Exception):
    """Raised by the fake sleep to end an otherwise endless watch loop."""


@pytest.mark.asyncio
async def test_configuring_a_disabled_policy_skips_model_pinning(monkeypatch):
    policy = TopologyPolicy(
        tenant_id="tenant", source_scope_id="scope", enabled=False, profile=_profile()
    )
    topology = SimpleNamespace(
        configure_policy=AsyncMock(return_value=3),
        reconcile=AsyncMock(return_value=0),
        get_policy=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        operations, "connect_topology_authority", _connect(SimpleNamespace(topology=topology))
    )
    pin = Mock()
    monkeypatch.setattr(operations, "pinned_configuration", pin)

    result = await operations.configure(RuntimeSettings(), policy)

    pin.assert_not_called()
    assert result == {
        "policy_revision": 3,
        "enqueued": 0,
        "enabled": False,
        "fingerprint": policy.fingerprint,
    }


def _worker_runtime(states):
    results = [SimpleNamespace(state=state) for state in states]
    service = SimpleNamespace(run_once=AsyncMock(side_effect=results))
    topology = SimpleNamespace(reconcile=AsyncMock(return_value=0))
    return SimpleNamespace(
        settings=RuntimeSettings(topology_poll_seconds=2),
        control=SimpleNamespace(topology=topology),
        service=service,
        derive=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_direct_run_reports_each_attempt_until_idle(monkeypatch):
    runtime = _worker_runtime(["accepted", "failed", "idle"])
    monkeypatch.setattr(operations, "connect_topology_runtime", _connect(runtime))
    monkeypatch.setattr(operations, "asdict", lambda result: {"state": result.state})

    reports = await operations.run(RuntimeSettings(), "tenant", limit=10)

    assert reports == [{"state": "accepted"}, {"state": "failed"}]
    runtime.control.topology.reconcile.assert_awaited_once_with("tenant")


@pytest.mark.asyncio
async def test_direct_run_stops_at_the_attempt_limit(monkeypatch):
    runtime = _worker_runtime(["accepted", "accepted", "accepted"])
    monkeypatch.setattr(operations, "connect_topology_runtime", _connect(runtime))
    monkeypatch.setattr(operations, "asdict", lambda result: {"state": result.state})

    reports = await operations.run(RuntimeSettings(), "tenant", limit=2)

    assert len(reports) == 2
    assert runtime.service.run_once.await_count == 2


@pytest.mark.asyncio
async def test_watch_mode_dispatches_derived_work_and_polls(monkeypatch):
    runtime = _worker_runtime(["accepted", "idle"])
    monkeypatch.setattr(operations, "connect_topology_runtime", _connect(runtime))
    dispatcher = SimpleNamespace(run_page=AsyncMock())
    dispatcher_type = Mock(return_value=dispatcher)
    monkeypatch.setattr(operations, "DerivedDispatcher", dispatcher_type)
    sleep = AsyncMock(side_effect=_StopWatching)
    monkeypatch.setattr(operations.asyncio, "sleep", sleep)

    with pytest.raises(_StopWatching):
        await operations.run(RuntimeSettings(), "tenant", watch=True)

    dispatcher_type.assert_called_once_with(
        runtime.control.topology, runtime.derive, runtime.settings
    )
    dispatcher.run_page.assert_awaited_once_with("tenant")
    sleep.assert_awaited_once_with(2)


def _frozen_build(chunk_ids=("chunk-1",)):
    content = TopologyBuildContent(build_id="build", job_id="job", chunk_ids=chunk_ids)
    payload = content.model_dump_json().encode()
    build = SimpleNamespace(
        artifact=SimpleNamespace(sha256=sha256(payload).hexdigest()),
        model_dump=lambda exclude: content.model_dump(),
    )
    return build, payload


def _rebuild_runtime(build, payload, lineage=True):
    repo = SimpleNamespace(
        get_build_lineage=AsyncMock(return_value=object() if lineage else None),
        get_build=AsyncMock(return_value=build),
    )
    return SimpleNamespace(
        control=SimpleNamespace(topology=repo),
        artifact_reader=SimpleNamespace(get=AsyncMock(return_value=payload)),
        projection=SimpleNamespace(write=AsyncMock(), verify=AsyncMock(return_value=True)),
    )


@pytest.mark.asyncio
async def test_rebuild_restores_a_matching_frozen_generation(monkeypatch):
    build, payload = _frozen_build()
    runtime = _rebuild_runtime(build, payload)
    monkeypatch.setattr(operations, "connect_topology_runtime", _connect(runtime))

    result = await operations.rebuild(RuntimeSettings(), "tenant", "build")

    assert result == {"build_id": "build", "verified": True, "eligible": True}
    runtime.projection.write.assert_awaited_once()
    assert runtime.projection.write.call_args.args == (build,)


@pytest.mark.asyncio
async def test_rebuild_refuses_ineligible_missing_tampered_or_diverged_builds(monkeypatch):
    build, payload = _frozen_build()
    settings = RuntimeSettings()

    async def rebuild_with(runtime):
        monkeypatch.setattr(operations, "connect_topology_runtime", _connect(runtime))
        await operations.rebuild(settings, "tenant", "build")

    with pytest.raises(ValueError, match="not currently eligible"):
        await rebuild_with(_rebuild_runtime(build, payload, lineage=False))
    with pytest.raises(ValueError, match="topology build is missing"):
        await rebuild_with(_rebuild_runtime(None, payload))
    with pytest.raises(ValueError, match="checksum mismatch"):
        await rebuild_with(_rebuild_runtime(build, payload + b" "))

    diverged, _ = _frozen_build(chunk_ids=("chunk-2",))
    diverged.artifact = build.artifact
    runtime = _rebuild_runtime(diverged, payload)
    with pytest.raises(ValueError, match="do not match frozen artifact"):
        await rebuild_with(runtime)
    runtime.projection.write.assert_not_awaited()


def _inspect_repo(**overrides):
    fields = {
        "eligible_build_ids": AsyncMock(return_value=("build",)),
        "get_build": AsyncMock(return_value=SimpleNamespace(job_id="job")),
        "get_job": AsyncMock(return_value=SimpleNamespace(policy=SimpleNamespace(profile=None))),
        "checkpoints": AsyncMock(return_value=()),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"eligible_build_ids": AsyncMock(return_value=())},
        {"get_build": AsyncMock(return_value=None)},
        {"get_job": AsyncMock(return_value=None)},
        {"eligible_build_ids": AsyncMock(side_effect=[("build",), ()])},
    ],
    ids=["ineligible", "missing-build", "missing-job", "revoked-during-inspection"],
)
@pytest.mark.asyncio
async def test_inspection_hides_every_unavailable_build_behind_one_error(monkeypatch, overrides):
    repo = _inspect_repo(**overrides)
    monkeypatch.setattr(
        operations, "connect_topology_authority", _connect(SimpleNamespace(topology=repo))
    )
    monkeypatch.setattr(operations, "_resolved_deployment", lambda *_args: {})
    builder = SimpleNamespace(build=Mock(return_value={"build_id": "build"}))
    monkeypatch.setattr(operations, "TopologyInspectionBuilder", Mock(return_value=builder))

    with pytest.raises(ValueError, match="^topology build is unavailable$"):
        await operations.inspect_build(
            RuntimeSettings(), "tenant", "build", principal_id="principal"
        )


@pytest.mark.asyncio
async def test_inspection_returns_the_builder_result_after_both_acl_checks(monkeypatch):
    repo = _inspect_repo()
    monkeypatch.setattr(
        operations, "connect_topology_authority", _connect(SimpleNamespace(topology=repo))
    )
    monkeypatch.setattr(operations, "_resolved_deployment", lambda *_args: {"match": 1})
    builder = SimpleNamespace(build=Mock(return_value={"build_id": "build"}))
    builder_type = Mock(return_value=builder)
    monkeypatch.setattr(operations, "TopologyInspectionBuilder", builder_type)

    result = await operations.inspect_build(
        RuntimeSettings(), "tenant", "build", principal_id="principal", limit=5
    )

    assert result == {"build_id": "build"}
    builder.build.assert_called_once_with(limit=5)
    assert builder_type.call_args.args[3] == {"match": 1}
    assert repo.eligible_build_ids.await_count == 2


def test_resolved_deployment_names_the_pinned_deployment(monkeypatch):
    deployment = SimpleNamespace(
        name="primary", provider=SimpleNamespace(value="openai"), model="gpt-test"
    )
    logical = SimpleNamespace(deployments=[deployment])
    config = SimpleNamespace(model_for=Mock(return_value=(None, logical)))
    monkeypatch.setattr(operations.HarborChatClientConfig, "from_file", lambda _path: object())
    monkeypatch.setattr(operations, "pinned_configuration", lambda *_args: config)

    result = operations._resolved_deployment(RuntimeSettings(), _profile())

    assert result == {
        "configuration_match": True,
        "deployment_name": "primary",
        "provider": "openai",
        "provider_model": "gpt-test",
    }
    config.model_for.assert_called_once_with("model")


@pytest.mark.parametrize("error", [KeyError("model"), OSError("gone"), ValueError("drift")])
def test_resolved_deployment_reports_no_match_when_configuration_drifted(monkeypatch, error):
    def fail(_path):
        raise error

    monkeypatch.setattr(operations.HarborChatClientConfig, "from_file", fail)

    assert operations._resolved_deployment(RuntimeSettings(), _profile()) == {
        "configuration_match": False
    }
    assert operations._resolved_deployment(SimpleNamespace(), _profile()) == {
        "configuration_match": False
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("x" * 20_000_001, "exceeds 20 million characters"),
        ("", "between 1 and 10000 units"),
        ("  \n\n  \n", "between 1 and 10000 units"),
        ("{}\n" * 10_001, "between 1 and 10000 units"),
    ],
    ids=["oversized", "empty", "blank-lines", "too-many-units"],
)
@pytest.mark.asyncio
async def test_offline_evaluation_rejects_out_of_bounds_input(payload, message):
    with pytest.raises(ValueError, match=message):
        await operations.evaluate(payload)
