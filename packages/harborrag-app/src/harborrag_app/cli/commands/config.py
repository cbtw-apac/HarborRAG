"""Import and export runtime settings from the terminal (`harborrag config ...`).

Each command calls the same ``SettingsImporter`` as the API's first-boot seed, in this
process, so validation, revisions and cache invalidation match the API. Dry run is the
default for ``import``; the control database stays the source of truth.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic_core import to_jsonable_python

from harborrag_app.cli.commands.auth_keys import operator_identity
from harborrag_app.cli.commands.ingest_run import JsonOption
from harborrag_app.cli.environment import refuse_checkout_write
from harborrag_app.cli.runner import invoke
from harborrag_app.cli.source_support import source_failure
from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.control_plane.settings_import import SettingsImporter
from harborrag_core.contracts.errors import HarborCapabilityError, HarborValidationError

app = typer.Typer(
    no_args_is_help=True,
    help="Import and export runtime settings: the overrides stored in the control database.",
)

Tenant = Annotated[
    str,
    typer.Option(
        "--tenant",
        metavar="TENANT_ID",
        envvar="HARBORRAG_INGESTION_TENANT_ID",
        help="Tenant whose settings to read or change.",
    ),
]

type ImporterCall = Callable[[SettingsImporter], Awaitable[Mapping[str, Any]]]


@app.command("import")
def import_settings(  # noqa: PLR0913 - one parameter per documented option
    context: typer.Context,
    file: Annotated[Path, typer.Argument(metavar="FILE", help="Settings YAML file.")],
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the changes; without it, print them only.")
    ] = False,
    prune: Annotated[
        bool, typer.Option("--prune", help="Also remove stored overrides the file lacks.")
    ] = False,
    tenant: Tenant = "DEFAULT",
    as_json: JsonOption = False,
) -> None:
    """Show what FILE would change, or write it as one revision with ``--apply``.

    Boot-only keys are reported and skipped. An invalid file exits 1, naming the file
    and the key, and changes nothing.
    """

    if apply:
        refuse_checkout_write()

    async def operation(importer: SettingsImporter) -> dict[str, Any]:
        from harborrag_runtime.config.settings_document import (  # noqa: PLC0415
            load_settings_document,
        )

        document = load_settings_document(file)
        plan = await importer.plan(tenant_id=tenant, document=document, prune=prune)
        revision = None
        if apply:
            revision = await importer.apply(plan, source="import", created_by=operator_identity())
        return {
            "applied": revision is not None,
            "revision": None if revision is None else f"rev_{revision}",
            "changes": [
                {
                    "level": change.level,
                    "key": change.key,
                    "from": change.from_value,
                    "to": change.to_value,
                    "applies": change.applies,
                }
                for change in plan.changes
            ],
            "skipped": [
                {"level": item.level, "key": item.key, "reason": item.reason}
                for item in plan.skipped
            ],
            "dry_run": not apply,
        }

    _invoke(context, "import", operation, as_json=as_json, subject=file)


@app.command("export")
def export_settings(
    context: typer.Context,
    level: Annotated[
        str | None,
        typer.Option(
            "--level",
            metavar="LEVEL",
            help="tenant, project:ID or connection:PROJECT/CONNECTION; default: every level.",
        ),
    ] = None,
    tenant: Tenant = "DEFAULT",
    output: Annotated[
        Path | None, typer.Option("--output", metavar="PATH", help="Write here, not to stdout.")
    ] = None,
) -> None:
    """Print the stored overrides as a settings file that ``import`` reads back unchanged.

    Overrides of a deleted project or connection are left out, each named on stderr.
    """

    if output is not None:
        refuse_checkout_write()

    async def operation(importer: SettingsImporter) -> dict[str, Any]:
        from harborrag_core.config_store import SettingsLevel  # noqa: PLC0415
        from harborrag_runtime.config.settings_document import (  # noqa: PLC0415
            dump_settings_document,
        )

        exported = await importer.export(
            tenant_id=tenant, level=None if level is None else SettingsLevel.parse(level)
        )
        for label in exported.left_out:
            typer.echo(f"harborrag: left out {label}: it no longer exists", err=True)
        text = dump_settings_document(exported.document)
        if output is None:
            return {"yaml": text}
        output.expanduser().write_text(text, encoding="utf-8")
        return {"output": str(output)}

    _invoke(context, "export", operation, as_json=False)


def _invoke(
    context: typer.Context,
    action: str,
    operation: ImporterCall,
    *,
    as_json: bool,
    subject: Path | None = None,
) -> None:
    """Run ``operation`` on the settings importer through the shared CLI runner."""
    invoke(
        _importer_call(operation, subject),
        context=context,
        command="config",
        action=action,
        as_json=as_json,
    )


def _importer_call(operation: ImporterCall, subject: Path | None) -> Callable[..., Any]:
    async def call(service: BaseAppService) -> AppResponse:
        from harborrag_runtime.config.settings_document import (  # noqa: PLC0415
            SettingsDocumentError,
        )

        try:
            importer: SettingsImporter | None = getattr(service, "settings_importer", None)
            if importer is None:
                raise HarborCapabilityError("this HarborRAG setup does not manage settings")
            return AppResponse(True, data=to_jsonable_python(dict(await operation(importer))))
        except SettingsDocumentError as error:
            return AppResponse(False, data={"error_type": type(error).__name__}, error=str(error))
        except HarborValidationError as error:
            return AppResponse(
                False,
                data={"error_type": type(error).__name__},
                error=_validation_message(error, subject),
            )
        except Exception as error:  # noqa: BLE001 - the CLI's stable error boundary
            return source_failure(error)

    return call


def _validation_message(error: HarborValidationError, subject: Path | None) -> str:
    """Name the file, key and level of a validation error, without values."""
    where = f"settings file {subject}: " if subject is not None else ""
    key = error.details.get("field")
    level = error.details.get("level")
    parts = [f"key {key}" if key else "", f"(level {level})" if level else "", str(error)]
    return where + " ".join(part for part in parts if part)
