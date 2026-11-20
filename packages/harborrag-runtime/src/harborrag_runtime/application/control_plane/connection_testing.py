"""Run the check chain of a connection test off the event loop, under a deadline.

Checks run on a small pool of their own, with no retries and short request timeouts, so a
source that never answers cannot hold the API's shared threads after the deadline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from harborrag_adapters.connectors.policies.network import DestinationPolicy
from harborrag_runtime.config.connectors.models import SourceTypeSpec, ValidatedConnection
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.ingestion.connection_checks import (
    ConnectionTestReport,
    run_connection_checks,
    timed_out_report,
)
from harborrag_runtime.ingestion.connections import build_connector_from_parts

from .connection_writes import merge_secrets

TEST_TIMEOUT_SECONDS = 15.0
TEST_REQUEST_TIMEOUT_SECONDS = 10.0
TEST_WORKERS = 4
_TEST_EXECUTOR = ThreadPoolExecutor(max_workers=TEST_WORKERS, thread_name_prefix="connection-test")


async def run_checks(  # noqa: PLR0913 - one connection test's inputs
    spec: SourceTypeSpec,
    validated: ValidatedConnection,
    effective: Mapping[str, Any],
    secret_values: Mapping[str, str],
    tenant_id: str,
    destination_policy: DestinationPolicy,
) -> ConnectionTestReport:
    """Build the connector and run its check chain, ending the wait at the deadline.

    Args:
        spec: The connection's source type.
        validated: The values that passed validation.
        effective: The credentials as they would be stored, secrets included.
        secret_values: The resolved secret values the connector reads.
        tenant_id: Tenant the connector is built for.
        destination_policy: Hosts the connector may reach beyond public addresses.

    Returns:
        The report of the checks, or a timed-out report when the deadline passed.
    """
    credentials = merge_secrets(validated.credentials, effective, spec.secret_fields)
    tuning = _test_tuning(spec, validated.tuning)

    def check() -> ConnectionTestReport:
        try:
            connector = build_connector_from_parts(
                spec.source_type,
                tenant_id=tenant_id,
                credentials=credentials,
                config=validated.config,
                tuning=tuning,
                secret_values=secret_values,
                destination_policy=destination_policy,
            )
        except ConnectorConfigurationError as error:
            report = ConnectionTestReport()
            report.passed("schema")
            report.failed("reachable", "unreachable", str(error))
            return report
        return run_connection_checks(connector)

    try:
        running = asyncio.get_running_loop().run_in_executor(_TEST_EXECUTOR, check)
        return await asyncio.wait_for(running, TEST_TIMEOUT_SECONDS)
    except TimeoutError:
        return timed_out_report()


def _test_tuning(spec: SourceTypeSpec, tuning: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``tuning`` for a connection test: no retries, no request past a short timeout."""
    capped = dict(tuning)
    if "max_retries" in spec.tuning_fields:
        capped["max_retries"] = 0
    if "request_timeout_seconds" in spec.tuning_fields:
        configured = capped.get("request_timeout_seconds")
        capped["request_timeout_seconds"] = min(
            configured or TEST_REQUEST_TIMEOUT_SECONDS, TEST_REQUEST_TIMEOUT_SECONDS
        )
    return capped
