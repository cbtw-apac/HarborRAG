"""Import and export runtime settings from the terminal (`harborrag config ...`).

Each command calls the same ``SettingsImporter`` as the API's first-boot seed, in this
process, so validation, revisions and cache invalidation match the API. Dry run is the
default for ``import``; the control database stays the source of truth.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic_core import to_jsonable_python

from harborrag_app.cli.commands.auth_keys import operator_identity
from harborrag_app.cli.environment import refuse_checkout_write
from harborrag_app.cli.globals import (
    ConfigOption,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    current_globals,
)
from harborrag_app.cli.invoke import Outcome, run_operation
from harborrag_app.operations.local import LocalOperations
from harborrag_app.operations.scope import Scope
from harborrag_app.workflow_control.control_plane.settings_import import SettingsImporter
from harborrag_core.contracts.errors import HarborValidationError

app = typer.Typer(
    no_args_is_help=True,
    help="Import and export runtime settings: the overrides stored in the control database.",
)

type ImporterCall = Callable[[SettingsImporter, str], Awaitable[Mapping[str, Any] | Outcome]]


@app.command("import")
def import_settings(  # noqa: PLR0913 - one parameter per documented option
    file: Annotated[Path, typer.Argument(metavar="FILE", help="Settings YAML file.")],
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the changes; without it, print them only.")
    ] = False,
    prune: Annotated[
        bool, typer.Option("--prune", help="Also remove stored overrides the file lacks.")
    ] = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Show what FILE would change, or write it as one revision with ``--apply``.

    Boot-only keys are reported and skipped. An invalid file exits 2, naming the file
    and the key, and changes nothing.
    """
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def operation(importer: SettingsImporter, tenant_id: str) -> dict[str, Any]:
        from harborrag_runtime.config.settings_document import (  # noqa: PLC0415
            load_settings_document,
        )

        document = load_settings_document(file)
        plan = await importer.plan(tenant_id=tenant_id, document=document, prune=prune)
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

    run_operation(None, _importer_call(operation, file), globals=resolved, write=apply)


@app.command("export")
def export_settings(  # noqa: PLR0913 - the level and file flags plus the global options
    level: Annotated[
        str | None,
        typer.Option(
            "--level",
            metavar="LEVEL",
            help="tenant, project:ID or connection:PROJECT/CONNECTION; default: every level.",
        ),
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", metavar="PATH", help="Write here, not to stdout.")
    ] = None,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Print the stored overrides as a settings file that ``import`` reads back unchanged.

    Overrides of a deleted project or connection are left out, each named on stderr.
    """
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    if file is not None:
        refuse_checkout_write()

    async def operation(importer: SettingsImporter, tenant_id: str) -> Mapping[str, Any] | Outcome:
        from harborrag_core.config_store import SettingsLevel  # noqa: PLC0415
        from harborrag_runtime.config.settings_document import (  # noqa: PLC0415
            dump_settings_document,
        )

        exported = await importer.export(
            tenant_id=tenant_id, level=None if level is None else SettingsLevel.parse(level)
        )
        for label in exported.left_out:
            typer.echo(f"harborrag: left out {label}: it no longer exists", err=True)
        text = dump_settings_document(exported.document)
        if file is None:
            sys.stdout.write(text)
            return Outcome(None, 0)
        file.expanduser().write_text(text, encoding="utf-8")
        return {"file": str(file)}

    run_operation(None, _importer_call(operation, None), globals=resolved)


def _importer_call(
    operation: ImporterCall, subject: Path | None
) -> Callable[[LocalOperations, Scope], Awaitable[object]]:
    """Run ``operation`` on the settings importer of the invocation's service."""

    async def call(operations: LocalOperations, scope: Scope) -> object:
        from harborrag_runtime.config.settings_document import (  # noqa: PLC0415
            SettingsDocumentError,
        )

        importer = operations.app_service.settings_importer
        try:
            result = await operation(importer, scope.tenant_id)
        except SettingsDocumentError as error:
            raise HarborValidationError(str(error)) from error
        except HarborValidationError as error:
            raise HarborValidationError(_validation_message(error, subject)) from error
        return result if isinstance(result, Outcome) else to_jsonable_python(dict(result))

    return call


def _validation_message(error: HarborValidationError, subject: Path | None) -> str:
    """Name the file, key and level of a validation error, without values."""
    where = f"settings file {subject}: " if subject is not None else ""
    key = error.details.get("field")
    level = error.details.get("level")
    parts = [f"key {key}" if key else "", f"(level {level})" if level else "", str(error)]
    return where + " ".join(part for part in parts if part)
