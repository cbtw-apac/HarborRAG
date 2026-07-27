"""Central task-queue names and activity routing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from harborrag_runtime.errors import RuntimeConfigurationError


class ActivityClass(StrEnum):
    """Workload classes with independently tunable retry and capacity policy."""

    DISCOVERY = "discovery"
    CONNECTOR = "connector"
    PARSER = "parser"
    OCR = "ocr"
    CHUNKING = "chunking"
    INDEXING = "indexing"
    VALIDATION = "validation"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True, slots=True)
class TaskQueueConfig:
    """All Temporal task queues used by the ingestion runtime."""

    discovery: str = "harborrag-runtime-discovery"
    connectors: str = "harborrag-runtime-connectors"
    parsers: str = "harborrag-runtime-parsers"
    ocr: str = "harborrag-runtime-ocr"
    chunking: str = "harborrag-runtime-chunking"
    vector_index: str = "harborrag-runtime-vector-index"
    graph_index: str = "harborrag-runtime-graph-index"
    maintenance: str = "harborrag-runtime-maintenance"

    def __post_init__(self) -> None:
        values = tuple(self.as_mapping().values())
        if any(not value.strip() for value in values):
            raise RuntimeConfigurationError("Temporal task-queue names must be non-empty")
        if len(set(values)) != len(values):
            raise RuntimeConfigurationError("Temporal task-queue names must be unique")

    def for_activity(self, activity_class: ActivityClass) -> str:
        """Return the queue for one activity workload class."""

        return {
            ActivityClass.DISCOVERY: self.discovery,
            ActivityClass.CONNECTOR: self.connectors,
            ActivityClass.PARSER: self.parsers,
            ActivityClass.OCR: self.ocr,
            ActivityClass.CHUNKING: self.chunking,
            ActivityClass.INDEXING: self.vector_index,
            ActivityClass.VALIDATION: self.graph_index,
            ActivityClass.MAINTENANCE: self.maintenance,
        }[activity_class]

    def as_mapping(self) -> dict[str, str]:
        """Return a sanitized mapping for diagnostics."""

        return {
            "discovery": self.discovery,
            "connectors": self.connectors,
            "parsers": self.parsers,
            "ocr": self.ocr,
            "chunking": self.chunking,
            "vector_index": self.vector_index,
            "graph_index": self.graph_index,
            "maintenance": self.maintenance,
        }
