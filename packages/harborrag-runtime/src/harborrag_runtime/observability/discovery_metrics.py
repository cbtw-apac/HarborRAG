"""Low-cardinality metrics for cursor-paged source discovery."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram


class DiscoveryMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self._pages = Counter(
            "harborrag_ingestion_discovery_pages_total",
            "Native connector discovery pages completed.",
            ("connector_type", "outcome"),
            registry=registry,
        )
        self._page_roots = Histogram(
            "harborrag_ingestion_discovery_page_roots",
            "Root source records returned per native discovery page.",
            ("connector_type",),
            buckets=(0, 1, 5, 10, 25, 50, 100, 200, 300),
            registry=registry,
        )
        self._page_duration = Histogram(
            "harborrag_ingestion_discovery_page_duration_seconds",
            "Time to fetch, describe, and register one native discovery page.",
            ("connector_type",),
            registry=registry,
        )

    def record(
        self,
        connector_type: str,
        *,
        root_count: int,
        duration_seconds: float,
        replayed: bool,
    ) -> None:
        self._pages.labels(
            connector_type=connector_type,
            outcome="replayed" if replayed else "fetched",
        ).inc()
        self._page_roots.labels(connector_type=connector_type).observe(max(0, root_count))
        if not replayed:
            self._page_duration.labels(connector_type=connector_type).observe(
                max(0.0, duration_seconds)
            )
