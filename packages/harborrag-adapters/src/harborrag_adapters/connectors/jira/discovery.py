from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from harborrag_adapters.connectors.attachments import (
    AttachmentSourceGateway,
    attachment_source_record,
    select_attachment_payloads,
)
from harborrag_adapters.connectors.descriptors import (
    ConnectorDocumentDescriptor,
)
from harborrag_adapters.connectors.policies.validation import (
    enforce_collection_limit,
)
from harborrag_core.chunking import RelationType
from harborrag_core.domain.source import SourceRecord
from harborrag_core.ingestion import (
    AdmissionSnapshot,
    SourceObjectVersion,
    SourceRelationDescriptor,
)

from .config import JiraProjectConfig
from .issues import DISCOVERY_DESCRIPTOR_KEY, JiraIssueAPI
from .mappers import build_source_record, issue_key_from_record, project_identity


class JiraDescriptorBuilder:
    """Build issue admission data and attachment records without attachment bytes."""

    def __init__(
        self,
        *,
        issues: JiraIssueAPI,
        attachments: AttachmentSourceGateway,
        config: JiraProjectConfig,
        base_url: str,
    ) -> None:
        self._issues = issues
        self._attachments = attachments
        self._config = config
        self._base_url = base_url

    def describe(
        self,
        record: SourceRecord,
    ) -> ConnectorDocumentDescriptor:
        issue_key = issue_key_from_record(record)
        cached_issue = record.metadata.get(DISCOVERY_DESCRIPTOR_KEY)
        issue = (
            cached_issue
            if isinstance(cached_issue, dict)
            else self._issues.get_issue_descriptor(issue_key)
        )
        discovered = build_source_record(issue, base_url=self._base_url)
        include_comments = bool(
            self._config.include_comments and record.metadata.get("include_comments", True)
        )
        comments = self._issues.fetch_comments(issue_key) if include_comments else []
        include_attachments = bool(
            self._config.include_attachments and record.metadata.get("include_attachments", True)
        )
        raw_attachments = []
        if include_attachments:
            raw_attachments = select_attachment_payloads(
                list(issue.get("fields", {}).get("attachment") or ()),
                tuple(
                    record.metadata.get(
                        "_selected_attachment_ids",
                        (),
                    )
                ),
            )
        enforce_collection_limit(
            count=len(raw_attachments),
            limit=self._config.max_attachments,
            label=f"JIRA attachments for {issue_key}",
            setting_name="max_attachments",
        )
        attachment_descriptors = self._attachments.describe(raw_attachments)
        # Same project identity the issue projects, so issue and attachment land under
        # one project node instead of the attachment falling back to the data source.
        bound_records = tuple(
            attachment_source_record(
                discovered,
                descriptor,
                inherited=project_identity(issue),
            )
            for descriptor in attachment_descriptors
            if descriptor.status == "admitted"
        )
        source_version = self._source_version(issue)
        relations, relation_payloads = self._relations(
            issue,
            attachments=bound_records,
            source_version=source_version,
        )
        source_metadata = {
            key: value
            for key, value in record.metadata.items()
            if key not in {"_selected_attachment_ids", DISCOVERY_DESCRIPTOR_KEY}
        }
        source = replace(
            record,
            updated_at=discovered.updated_at,
            metadata={
                **source_metadata,
                **discovered.metadata,
                "source_version": source_version,
                "defer_attachments": True,
                "attachment_names": [descriptor.title for descriptor in attachment_descriptors],
                "relations": relation_payloads,
            },
        )
        return ConnectorDocumentDescriptor(
            source=source,
            admission=AdmissionSnapshot(
                source_version=source_version,
                comments=tuple(
                    SourceObjectVersion(
                        source_item_id=str(comment.get("id") or ""),
                        source_version=self._comment_version(comment),
                    )
                    for comment in comments
                    if str(comment.get("id") or "").strip()
                ),
                attachments=tuple(
                    SourceObjectVersion(
                        source_item_id=descriptor.attachment_id,
                        source_version=descriptor.source_version,
                    )
                    for descriptor in attachment_descriptors
                ),
                relations=relations,
            ),
            bound_records=bound_records,
        )

    @staticmethod
    def _source_version(issue: dict[str, Any]) -> str:
        value = issue.get("fields", {}).get("updated")
        if value is None or not str(value).strip():
            raise ValueError("JIRA descriptor has no source version")
        return str(value)

    @staticmethod
    def _comment_version(comment: dict[str, Any]) -> str:
        value = comment.get("updated") or comment.get("created")
        if value is not None and str(value).strip():
            return str(value)
        return hashlib.sha256(
            json.dumps(
                {
                    "id": comment.get("id"),
                    "status": comment.get("visibility"),
                },
                default=str,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _relations(
        cls,
        issue: dict[str, Any],
        *,
        attachments: tuple[SourceRecord, ...],
        source_version: str,
    ) -> tuple[
        tuple[SourceRelationDescriptor, ...],
        list[dict[str, object]],
    ]:
        fields = issue.get("fields") or {}
        values: list[tuple[SourceRelationDescriptor, str]] = []
        parent_issue = fields.get("parent")
        if isinstance(parent_issue, dict) and parent_issue.get("key"):
            target = cls._source_id(str(parent_issue["key"]))
            values.append(
                (
                    SourceRelationDescriptor(
                        relation_type=RelationType.CHILD_OF,
                        target_source_item_id=target,
                        source_relation_version=source_version,
                    ),
                    "child_of",
                )
            )
        for link in fields.get("issuelinks") or ():
            relation = cls._issue_link(link, source_version=source_version)
            if relation is not None:
                values.append(relation)
        values.extend(
            (
                SourceRelationDescriptor(
                    relation_type=RelationType.HAS_ATTACHMENT,
                    target_source_item_id=attachment.id,
                    source_relation_version=str(attachment.metadata["source_version"]),
                ),
                "has_attachment",
            )
            for attachment in attachments
        )
        descriptors = tuple(value[0] for value in values)
        payloads: list[dict[str, object]] = [
            {
                "predicate": predicate,
                "target_id": descriptor.target_source_item_id,
                "target_type": "document",
                "metadata": {"source_relation_version": (descriptor.source_relation_version)},
            }
            for descriptor, predicate in values
        ]
        return descriptors, payloads

    @classmethod
    def _issue_link(
        cls,
        link: dict[str, Any],
        *,
        source_version: str,
    ) -> tuple[SourceRelationDescriptor, str] | None:
        outward = link.get("outwardIssue")
        inward = link.get("inwardIssue")
        target = outward or inward
        if not isinstance(target, dict) or not target.get("key"):
            return None
        link_name = str((link.get("type") or {}).get("name") or "")
        normalized = link_name.casefold().replace(" ", "_")
        relation_type = {
            "blocks": RelationType.BLOCKS,
            "duplicate": RelationType.DUPLICATES,
            "duplicates": RelationType.DUPLICATES,
        }.get(normalized, RelationType.RELATES_TO)
        predicate = relation_type.value
        if inward is not None and relation_type == RelationType.BLOCKS:
            predicate = "is_blocked_by"
        elif inward is not None and relation_type == RelationType.DUPLICATES:
            predicate = "is_duplicated_by"
        return (
            SourceRelationDescriptor(
                relation_type=relation_type,
                target_source_item_id=cls._source_id(str(target["key"])),
                source_relation_version=str(link.get("id") or source_version),
            ),
            predicate,
        )

    @staticmethod
    def _source_id(issue_key: str) -> str:
        return f"jira://{issue_key.split('-', 1)[0]}/{issue_key}"
