"""Durable summary authority composed from focused SQL operations."""

from .summary_cache import SummaryCacheOperations
from .summary_jobs import SummaryJobOperations
from .summary_maintenance import SummaryMaintenanceOperations
from .summary_products import SummaryProductOperations
from .summary_publication import SummaryPublicationOperations
from .summary_reads import SummaryReadOperations


class SummaryRepository(
    SummaryJobOperations,
    SummaryReadOperations,
    SummaryProductOperations,
    SummaryCacheOperations,
    SummaryPublicationOperations,
    SummaryMaintenanceOperations,
):
    """Summary jobs, immutable generation cache, acceptance and authorized serving."""
