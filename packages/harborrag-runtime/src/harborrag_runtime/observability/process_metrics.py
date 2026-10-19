"""Expose ingestion worker CPU, including its isolated parse subprocesses."""

from __future__ import annotations

from collections.abc import Iterator

from prometheus_client import CollectorRegistry, ProcessCollector
from prometheus_client.core import CounterMetricFamily, Metric

try:
    import resource
except ImportError:  # Windows has no getrusage; the collector then reports nothing.
    resource = None  # type: ignore[assignment]


class SubprocessCpuCollector:
    """Report CPU spent by finished child processes the worker has waited for.

    ``ProcessCollector`` reads only the worker's own utime/stime, so parsing,
    which runs through ``run_in_isolated_subprocess``, would otherwise be
    invisible. The kernel credits a child's CPU to ``RUSAGE_CHILDREN`` once the
    parent joins it, so this counter grows as each isolated stage completes.
    """

    def collect(self) -> Iterator[Metric]:
        if resource is None:
            return
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        family = CounterMetricFamily(
            "harborrag_ingestion_subprocess_cpu_seconds",
            "User and system CPU time of finished isolated worker subprocesses.",
        )
        family.add_metric([], usage.ru_utime + usage.ru_stime)
        yield family


def register_process_metrics(registry: CollectorRegistry) -> None:
    """Add process CPU/memory and subprocess CPU collectors to ``registry``."""

    ProcessCollector(registry=registry)
    registry.register(SubprocessCpuCollector())
