"""Source-scan, removal, and retirement behaviour of the ingestion control plane.

Split from test_ingestion_control_plane.py, which covers publication and versioning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.contracts import HarborConflictError
from harborrag_core.ingestion import (
    BindingKind,
    DiscoveredSourceItem,
    SourceAdmissionDecision,
    SourceBinding,
    SourceIdentity,
)

from .ingestion_control_fixtures import (
    advance_to_verified,
    candidate,
    make_control_plane,
    source_identity,
)


@pytest.mark.asyncio
async def test_source_scan_overlap_and_failed_scan_cannot_remove_documents(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        scans = control_plane.source_scans
        await scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config-v1",
        )
        failed_scan = await scans.start("scope-engineering")
        with pytest.raises(HarborConflictError, match="already running"):
            await scans.start("scope-engineering")
        await scans.fail(failed_scan, safe_reason="connector_timeout")
        await scans.fail(failed_scan, safe_reason="connector_timeout")

        with pytest.raises(HarborConflictError, match="completed scan"):
            await scans.reconcile_removals(failed_scan)


@pytest.mark.asyncio
async def test_open_scan_start_is_idempotent_for_the_same_identity(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        scans = control_plane.source_scans
        await scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config-v1",
        )

        first = await scans.start(
            "scope-engineering",
            scan_id="scan:deterministic",
        )
        replay = await scans.start(
            "scope-engineering",
            scan_id="scan:deterministic",
        )

        assert replay == first
        with pytest.raises(HarborConflictError, match="already running"):
            await scans.start(
                "scope-engineering",
                scan_id="scan:different",
            )


@pytest.mark.asyncio
async def test_source_scan_records_a_batch_with_aligned_decisions(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        scans = control_plane.source_scans
        await scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config-v1",
        )
        scan_id = await scans.start("scope-engineering")
        values = tuple(
            candidate(value, source=source_identity(f"page-{index}"))
            for index, value in enumerate(("first", "second"), start=1)
        )
        items = tuple(
            DiscoveredSourceItem(
                source_identity=value.source_identity,
                document_id=value.document_id,
                source_version=str(index),
                admission_change_key=value.fingerprints.admission_change_key,
                descriptor={"index": index},
            )
            for index, value in enumerate(values, start=1)
        )

        first = await scans.record_seen_many(scan_id=scan_id, items=items)
        replay = await scans.record_seen_many(scan_id=scan_id, items=items)

        assert tuple(item.decision for item in first) == (
            SourceAdmissionDecision.NEW,
            SourceAdmissionDecision.NEW,
        )
        assert tuple(item.decision for item in replay) == (
            SourceAdmissionDecision.UNCHANGED,
            SourceAdmissionDecision.UNCHANGED,
        )


@pytest.mark.asyncio
async def test_older_completed_scan_cannot_increment_removal_misses(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        scans = control_plane.source_scans
        await scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config-v1",
        )
        value = candidate("published")
        older_scan = await scans.start("scope-engineering")
        await scans.record_seen(
            scan_id=older_scan,
            item=DiscoveredSourceItem(
                source_identity=value.source_identity,
                document_id=value.document_id,
                source_version="1",
                admission_change_key=value.fingerprints.admission_change_key,
            ),
        )
        await scans.complete(older_scan)
        newer_scan = await scans.start("scope-engineering")
        await scans.complete(newer_scan)

        assert await scans.reconcile_removals(older_scan) == ()
        assert await scans.reconcile_removals(newer_scan) == ()
        stored = await scans.source_item(
            source_scope_id="scope-engineering",
            source_item_id=value.source_identity.source_item_id,
        )
        assert stored is not None
        assert stored.active is True


@pytest.mark.asyncio
async def test_removal_requires_consecutive_successful_scan_misses(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        scans = control_plane.source_scans
        await scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config-v1",
        )
        first_scan = await scans.start("scope-engineering")
        first_candidate = candidate("first")
        await scans.record_seen(
            scan_id=first_scan,
            item=DiscoveredSourceItem(
                source_identity=first_candidate.source_identity,
                document_id=first_candidate.document_id,
                source_version="1",
                admission_change_key=first_candidate.fingerprints.admission_change_key,
            ),
        )
        await scans.complete(first_scan)
        assert await scans.reconcile_removals(first_scan) == ()

        second_scan = await scans.start("scope-engineering")
        await scans.complete(second_scan)
        assert await scans.reconcile_removals(second_scan) == ()

        third_scan = await scans.start("scope-engineering")
        await scans.complete(third_scan)
        assert await scans.reconcile_removals(third_scan) == (str(first_candidate.document_id),)
        assert await scans.reconcile_removals(third_scan) == ()
        stored = await scans.source_item(
            source_scope_id="scope-engineering",
            source_item_id=first_candidate.source_identity.source_item_id,
        )
        assert stored is not None
        assert stored.active is False


@pytest.mark.asyncio
async def test_disabled_attachment_binding_is_removed_after_one_completed_scan(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        scans = control_plane.source_scans
        await scans.register_scope(
            source_scope_id="scope-engineering",
            connector_type="confluence",
            connection_id="wiki.example",
            configuration_fingerprint="config-v1",
        )
        attachment_source = SourceIdentity(
            connector_type=source_identity().connector_type,
            connection_id="wiki.example",
            source_item_id="attachment-1",
            source_scope_id="scope-engineering",
            binding=SourceBinding(
                kind=BindingKind.ATTACHMENT,
                parent_source_item_id="page-1",
            ),
        )
        attachment = candidate("attachment", source=attachment_source)
        initial = await scans.start("scope-engineering")
        await scans.record_seen(
            scan_id=initial,
            item=DiscoveredSourceItem(
                source_identity=attachment.source_identity,
                document_id=attachment.document_id,
                source_version="1",
                admission_change_key=attachment.fingerprints.admission_change_key,
            ),
        )
        await scans.complete(initial)
        assert await scans.reconcile_removals(initial) == ()

        disabled_scan = await scans.start("scope-engineering")
        await scans.complete(disabled_scan)

        assert await scans.reconcile_removals(
            disabled_scan,
            immediate_binding_kinds={BindingKind.ATTACHMENT.value},
        ) == (str(attachment.document_id),)


@pytest.mark.asyncio
async def test_overlapping_scopes_share_document_without_early_retirement(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        scans = control_plane.source_scans
        for scope_id in ("scope-a", "scope-b"):
            await scans.register_scope(
                source_scope_id=scope_id,
                connector_type="confluence",
                connection_id="wiki.example",
                configuration_fingerprint=f"{scope_id}-config",
            )
        base = source_identity()
        identities = (
            SourceIdentity(
                connector_type=base.connector_type,
                connection_id=base.connection_id,
                source_item_id=base.source_item_id,
                source_scope_id=scope_id,
            )
            for scope_id in ("scope-a", "scope-b")
        )
        shared = tuple(candidate("shared", source=value) for value in identities)
        assert shared[0].document_id == shared[1].document_id
        for value in shared:
            scan_id = await scans.start(value.source_identity.source_scope_id)
            await scans.record_seen(
                scan_id=scan_id,
                item=DiscoveredSourceItem(
                    source_identity=value.source_identity,
                    document_id=value.document_id,
                    source_version="1",
                    admission_change_key=value.fingerprints.admission_change_key,
                ),
            )
            await scans.complete(scan_id)

        first_missing = await scans.start("scope-a")
        await scans.complete(first_missing)
        assert (
            await scans.reconcile_removals(
                first_missing,
                missing_threshold=1,
            )
            == ()
        )

        second_missing = await scans.start("scope-b")
        await scans.complete(second_missing)
        assert await scans.reconcile_removals(
            second_missing,
            missing_threshold=1,
        ) == (str(shared[0].document_id),)


@pytest.mark.asyncio
async def test_removed_document_retirement_is_atomic_and_idempotent(
    tmp_path: Path,
) -> None:
    control_plane = make_control_plane(tmp_path)
    async with control_plane:
        value = candidate("published then removed")
        await advance_to_verified(control_plane, value)
        await control_plane.publisher.publish(
            document_id=str(value.document_id),
            candidate_document_version_id=str(value.document_version_id),
        )

        retired = await control_plane.publisher.retire_removed(document_id=str(value.document_id))
        replayed = await control_plane.publisher.retire_removed(document_id=str(value.document_id))

        assert retired.retired_document_version_id == value.document_version_id
        assert retired.cleanup_job_created is True
        assert replayed.replayed is True
        assert await control_plane.document_versions.active_versions([str(value.document_id)]) == {}
        absent = await control_plane.publisher.retire_removed(
            document_id="never-published-document"
        )
        assert absent.replayed is True
        assert absent.retired_document_version_id is None
