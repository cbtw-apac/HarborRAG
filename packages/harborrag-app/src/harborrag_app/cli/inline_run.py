"""``ingestions start --inline``: run one ingestion in this process, without Temporal.

Handles:
- Resolving ``PROJECT_ID/CONNECTION_ID`` to the stored connection and its ``source_key``
- Refusing a ``local_file`` connection whose ``source_path`` is not a directory on this
  machine, before any work, since the stack stores the path its containers see
- Running the direct ingestion and choosing the exit code from the result's status

Does NOT import ``connectors.yaml``: the one-time import stays in the API's start-up, and the
connection must already exist. For development only; production runs go through Temporal.

Start from: run_inline.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from harborrag_app.cli.exit_codes import RUN_FAILED
from harborrag_app.cli.invoke import DEBUG_HINT, Outcome
from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.scope import ConnectionRef, Scope
from harborrag_app.workflow_control.ingestion.direct import new_run_id
from harborrag_core.contracts.errors import HarborError, HarborValidationError
from harborrag_core.domain.connection import Connection
from harborrag_core.ingestion import IngestionTaskState

if TYPE_CHECKING:
    from harborrag_app.workflow_control.composition.service import AppService

LOCAL_FILE = "local_file"
SUCCEEDED_STATES = frozenset({IngestionTaskState.COMPLETED, IngestionTaskState.PARTIAL})


async def run_inline(
    service: AppService,
    scope: Scope,
    ref: ConnectionRef,
    *,
    mode: str,
    updated_after: str | None,
) -> Outcome:
    """Run an ingestion of a stored connection in this process and return its result.

    Args:
        service: Application service whose direct ingestion runs the pipeline.
        scope: Caller and tenant; the caller needs the role ``start_ingestion`` needs.
        ref: The connection to ingest.
        mode: ``incremental``, or ``force`` to reprocess unchanged items.
        updated_after: ISO-8601 time with an offset; only items changed since then.

    Returns:
        The run's result, ``{task_id, status, discovered, published, unchanged, failed}``,
        where ``status`` is an ``IngestionTaskState`` value; exit ``0`` when it is
        ``COMPLETED`` or ``PARTIAL``, else ``8``.

    Raises:
        HarborNotFoundError: No such connection in the tenant.
        HarborValidationError: A ``local_file`` source path is not a directory here.
        HarborError: The run failed to run; the message is the reviewed public one.
    """
    scope.require(OPERATIONS["start_ingestion"].role)
    connection = await service.connection_service.stored(ref.key(scope.tenant_id))
    _refuse_a_source_path_not_here(connection)
    response = await service.run_ingestion(
        tenant_id=scope.tenant_id,
        connector_name=connection.source_key,
        run_id=new_run_id(),
        updated_after=updated_after,
        force_reprocess=mode == "force",
    )
    if not response.ok:
        raise HarborError(f"the inline run failed: {response.error}{DEBUG_HINT}")
    result = response.data["result"]
    status = result.get("status") if isinstance(result, dict) else None
    return Outcome(result, 0 if status in SUCCEEDED_STATES else RUN_FAILED)


def _refuse_a_source_path_not_here(connection: Connection) -> None:
    """Refuse a ``local_file`` connection whose ``source_path`` is not a directory here.

    Raises:
        HarborValidationError: The path is missing or not a directory on this machine.
    """
    source_path = connection.config.get("source_path")
    if connection.source_type != LOCAL_FILE or not isinstance(source_path, str):
        return
    if Path(source_path).expanduser().is_dir():
        return
    issue = f"{source_path} is not a directory on this machine; --inline reads files from here"
    raise HarborValidationError(
        f"connection {connection.ref} cannot run inline",
        {"errors": [{"field": "config.source_path", "issue": issue}]},
    )
