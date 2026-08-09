"""Run bounded live Confluence and Jira ingestion without exposing content."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from uuid import uuid4

from configuration import ROOT, load_smoke_configuration
from configured_inspection import (
    ConfiguredInspectionRequest,
    inspect_configured_source,
)

from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_runtime.config import (
    load_connector_catalog,
    load_parser_catalog,
)
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.client import (
    IngestionTemporalClient,
)
from harborrag_runtime.temporal.schemas import (
    SourceIngestionInput,
    SourceQuery,
)

_MAX_LIVE_ATTACHMENTS = 2
_SELECTION_WINDOW = 10


@dataclass(frozen=True, slots=True)
class SourceSelection:
    provider: str
    source_item_id: str
    locator: str
    title: str
    available_comments: int
    available_attachments: int
    selected_attachment_ids: tuple[str, ...]

    @property
    def include_attachments(self) -> bool:
        return bool(self.selected_attachment_ids)

    @property
    def tenant_id(self) -> str:
        return f"configured-{self.provider}-smoke"

    @property
    def connection_id(self) -> str:
        return f"configured-{self.provider}"

    @property
    def source_scope_id(self) -> str:
        digest = sha256()
        digest.update(self.source_item_id.encode("utf-8"))
        for attachment_id in sorted(self.selected_attachment_ids):
            digest.update(b"\0")
            digest.update(attachment_id.encode("utf-8"))
        return f"configured-{self.provider}-smoke-{digest.hexdigest()[:16]}"

    @property
    def expected_documents(self) -> int:
        return 1 + len(self.selected_attachment_ids)

    @property
    def filters_json(self) -> str:
        key = "content_ids" if self.provider == "confluence" else "issue_keys"
        filters = {key: [self.locator]}
        if self.selected_attachment_ids:
            filters["attachment_ids"] = list(self.selected_attachment_ids)
        return json.dumps(filters, sort_keys=True)


async def run_configured_smoke() -> dict[str, object]:
    configuration = load_smoke_configuration()
    run_token = uuid4().hex
    configuration = replace(
        configuration,
        processing=replace(
            configuration.processing,
            graph_projection_version=(f"structural-graph-configured-smoke-{run_token}"),
        ),
    )
    selections = await asyncio.to_thread(_select_sources)
    temporal = await IngestionTemporalClient.connect(
        TemporalRuntimeConfig.from_settings(configuration.settings)
    )
    reports = {}
    for selected in selections:
        task_id = f"{selected.provider}-smoke-{uuid4().hex}"
        source = _source_input(
            selected,
            task_id=task_id,
            processing=configuration.processing,
        )
        handle = await temporal.start(source)
        result = await handle.result()
        _assert_result(selected, result)
        history = await handle.fetch_history()
        status = await temporal.execution_status(task_id)
        inspected = await inspect_configured_source(
            ConfiguredInspectionRequest(
                settings=configuration.settings,
                task_id=task_id,
                tenant_id=selected.tenant_id,
                connector_type=selected.provider,
                connection_id=selected.connection_id,
                source_item_id=selected.source_item_id,
                dense_query=selected.title,
                sparse_query=selected.locator,
                expected_documents=result.discovered,
            )
        )
        replay_id = f"{selected.provider}-replay-{uuid4().hex}"
        replay = await (
            await temporal.start(
                _source_input(
                    selected,
                    task_id=replay_id,
                    processing=configuration.processing,
                )
            )
        ).result()
        if replay.unchanged != replay.discovered or replay.failed:
            raise AssertionError(f"{selected.provider} unchanged replay reprocessed content")
        reports[selected.provider] = {
            "selection": {
                "source_id_hash": _short_hash(selected.source_item_id),
                "available_comments": selected.available_comments,
                "available_attachments": (selected.available_attachments),
                "scheduled_attachments": (len(selected.selected_attachment_ids)),
            },
            "temporal": {
                "status": status,
                "history_events": len(history.events),
                "history_bytes": sum(event.ByteSize() for event in history.events),
                "result": asdict(result),
                "unchanged_replay": asdict(replay),
            },
            "stores": inspected,
        }
    return reports


def _select_sources() -> tuple[SourceSelection, ...]:
    configuration = load_smoke_configuration()
    parser = load_parser_catalog(configuration.settings.parser_config_path).build_harbor_parser()
    catalog = load_connector_catalog(configuration.settings.connector_config_path)
    selections = []
    for provider in ("confluence", "jira"):
        connector = catalog.build(
            provider,
            connector_kwargs={"parser": parser},
        )
        try:
            connector.connect()
            selections.append(
                _select_provider_source(
                    provider,
                    connector,
                )
            )
        finally:
            connector.close()
    return tuple(selections)


def _select_provider_source(provider: str, connector) -> SourceSelection:
    query = ConnectorQuery(
        limit=_SELECTION_WINDOW,
        include_attachments=True,
    )
    candidates = []
    for record in connector.discover(query):
        descriptor = connector.describe(record)
        attachments = len(descriptor.bound_records)
        candidate = SourceSelection(
            provider=provider,
            source_item_id=descriptor.source.id,
            locator=descriptor.source.locator,
            title=str(descriptor.source.metadata.get("title") or descriptor.source.locator).strip(),
            available_comments=len(descriptor.admission.comments),
            available_attachments=attachments,
            selected_attachment_ids=tuple(
                record.locator for record in descriptor.bound_records[:_MAX_LIVE_ATTACHMENTS]
            ),
        )
        candidates.append(candidate)
        if _selection_priority(candidate) == 0:
            return candidate
    if not candidates:
        raise AssertionError(f"configured {provider} source returned no documents")
    return min(candidates, key=_selection_priority)


def _selection_priority(selection: SourceSelection) -> int:
    has_attachments = bool(selection.selected_attachment_ids)
    if selection.available_comments and has_attachments:
        return 0
    if has_attachments:
        return 1
    if selection.available_comments:
        return 2
    if selection.available_attachments == 0:
        return 3
    return 4


def _source_input(
    selected: SourceSelection,
    *,
    task_id: str,
    processing,
) -> SourceIngestionInput:
    return SourceIngestionInput(
        task_id=task_id,
        tenant_id=selected.tenant_id,
        connector_name=selected.provider,
        connector_type=selected.provider,
        connection_id=selected.connection_id,
        source_scope_id=selected.source_scope_id,
        configuration_fingerprint=_configuration_fingerprint(selected),
        processing=processing,
        query=SourceQuery(
            limit=1,
            include_attachments=selected.include_attachments,
            filters_json=selected.filters_json,
        ),
        document_concurrency=3,
        batch_size=100,
    )


def _configuration_fingerprint(
    selected: SourceSelection,
) -> str:
    digest = sha256()
    digest.update((ROOT / "config/connectors.yaml").read_bytes())
    digest.update(b"\0")
    digest.update(selected.source_item_id.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(selected.include_attachments).encode("ascii"))
    for attachment_id in sorted(selected.selected_attachment_ids):
        digest.update(b"\0")
        digest.update(attachment_id.encode("utf-8"))
    return f"configured-smoke-{digest.hexdigest()[:24]}"


def _assert_result(selected: SourceSelection, result) -> None:
    if result.failed:
        raise AssertionError(f"{selected.provider} ingestion failed documents")
    if result.discovered != selected.expected_documents:
        raise AssertionError(f"{selected.provider} discovery count changed during smoke")
    if result.published != result.discovered:
        raise AssertionError(f"{selected.provider} first run did not publish every document")


def _short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    try:
        report = asyncio.run(run_configured_smoke())
    except Exception as error:
        print(f"Configured-source ingestion smoke failed: {type(error).__name__}")
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    print("Configured-source ingestion smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
