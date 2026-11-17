"""Issue, list and revoke MCP reader keys.

The secret is written once to a file the operator names, created with
``O_EXCL`` and mode 0600, and never printed: terminal scrollback, shell
recordings and CI logs all outlive a key's usefulness. Operator notices go through
the CLI log, which the kernel holds until the command succeeds, so a failure's stderr
is its error report alone.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import socket
import stat
from collections.abc import Awaitable
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from harborrag_app.cli.globals import (
    GLOBAL_OPTIONS_PANEL,
    TENANT_ENV,
    ConfigOption,
    OutputOption,
    ProfileOption,
    TenantOption,
    YesOption,
    current_globals,
)
from harborrag_app.cli.invoke import LocalSetupError, ReportedError, run_standalone
from harborrag_app.cli.project import invocation_path
from harborrag_app.cli.typer_app import new_typer
from harborrag_app.operations.errors import ErrorBody
from harborrag_core.invariants import HarborInvariantError

logger = logging.getLogger("harborrag.app.cli.auth_keys")

app = new_typer(no_args_is_help=True, help="Hashed, tenant-bound MCP reader keys with a TTL.")

_DURATION_RE = re.compile(r"^(\d{1,6})([dhm])$")  # bounded: no OverflowError from timedelta
_UNITS = {"d": timedelta(days=1), "h": timedelta(hours=1), "m": timedelta(minutes=1)}


def parse_duration(value: str) -> timedelta:
    """``90d``, ``12h`` or ``30m``; the key policy applies its own limits."""

    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise typer.BadParameter("use a number followed by d, h or m, for example 30d or 12h")
    return int(match.group(1)) * _UNITS[match.group(2)]


def operator_identity() -> str:
    """Who ran the command, for the audit trail; access control is the DB credential."""

    return f"{getpass.getuser()}@{socket.gethostname()}"


DEFAULT_KEY_DIR = Path("~/.harborrag/keys")


def default_secret_output(name: str) -> Path:
    """``~/.harborrag/keys/<name>.key``: private to the operator, created on demand."""

    return (DEFAULT_KEY_DIR / f"{name}.key").expanduser()


def check_secret_output(path: Path) -> None:
    """Prepare a safe destination before a key exists to deliver.

    A missing parent directory is created owner-only rather than refused: the
    common first run is a fresh operator machine. An existing directory that
    other users can write to is still refused, and so is an existing file.
    """

    if path.exists():
        raise typer.BadParameter(f"{path} already exists; choose a new file for the secret")
    parent = path.resolve().parent
    if not parent.exists():
        try:
            parent.mkdir(mode=0o700, parents=True)
        except OSError as error:
            raise typer.BadParameter(f"cannot create {parent}: {type(error).__name__}") from None
    elif not parent.is_dir():
        raise typer.BadParameter(f"{parent} is not a directory")
    if parent.stat().st_mode & stat.S_IWOTH:
        raise typer.BadParameter(f"{parent} is world-writable; use a private directory")


def write_secret_file(path: Path, secret: str) -> None:
    # O_EXCL: fail if the file appeared meanwhile. 0600: owner-only from the start.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(secret + "\n")


async def _guarded[T](operation: Awaitable[T]) -> T:
    """Await a key-store call, raising the failures an operator can fix as ``ReportedError``.

    Every message here is authored in this repository and names no input or secret; any other
    exception reaches the error boundary, which reports it without its text.
    """
    from harborrag_core.ports.api_keys import AuthStoreUnavailable
    from harborrag_engine.security import ApiKeyPolicyError
    from harborrag_runtime.security.api_key_operations import (
        ApiKeyRejected,
        ApiKeySchemaBehind,
        ApiKeySchemaMissing,
        ApiKeySchemaUnstamped,
    )

    try:
        return await operation
    except ApiKeyPolicyError as error:
        raise ReportedError(ErrorBody(422, "validation_error", str(error))) from error
    except (ApiKeySchemaMissing, ApiKeySchemaBehind, ApiKeySchemaUnstamped) as error:
        raise ReportedError(ErrorBody(503, "not_ready", str(error))) from error
    except ApiKeyRejected as error:
        raise ReportedError(ErrorBody(409, "invalid_state", str(error))) from error
    except AuthStoreUnavailable as error:
        raise ReportedError(ErrorBody(503, "dependency_unavailable", str(error))) from error
    except Exception as error:
        from sqlalchemy.exc import IntegrityError

        if isinstance(error, IntegrityError):
            rejected = ErrorBody(409, "invalid_state", str(ApiKeyRejected()))
            raise ReportedError(rejected) from None
        raise


def settings_overrides(root: Path | None) -> dict[str, Any]:
    """Connection settings a checkout supplies when the environment does not.

    The CLI never loads ``env/.env.database`` wholesale (it points services at
    in-cluster hostnames), but the control database is exactly what these
    commands need: derive the loopback URL from it the way the MCP launcher
    does, and take ``HARBORRAG_ENV`` from ``env/.env.api`` so an issued key
    carries the environment the server runs with. An exported variable wins.
    """

    from harborrag_runtime.config.checkout import (
        checkout_control_db_url,
        checkout_environment_name,
    )

    overrides: dict[str, Any] = {}
    if root is None:
        return overrides
    if "HARBORRAG_CONTROL_DB_URL" not in os.environ:
        url = checkout_control_db_url(root)
        if url:
            overrides["control_db_url"] = url
    if "HARBORRAG_ENV" not in os.environ:
        name = checkout_environment_name(root)
        if name:
            overrides["env"] = name
    return overrides


def _settings() -> Any:
    """Runtime settings for the control database.

    Raises:
        LocalSetupError: The settings are invalid; reported as the operator's configuration error.
    """

    from harborrag_app.cli.project import find_legacy_checkout
    from harborrag_runtime.config.settings import RuntimeSettings

    try:
        settings = RuntimeSettings(**settings_overrides(find_legacy_checkout()))
    except Exception as error:
        raise LocalSetupError(error) from error
    if settings.control_db_url.get_secret_value().lower().startswith("sqlite"):
        logger.warning(
            "the control database is the local SQLite default; run from the "
            "checkout root (env/.env.database) or export HARBORRAG_CONTROL_DB_URL so keys land "
            "in the database the MCP server reads"
        )
    return settings


@app.command("create")
def create(  # noqa: PLR0913 - one parameter per documented option, plus the global options
    owner: Annotated[str, typer.Option("--owner", help="user-<name>, svc-<name> or team-<name>.")],
    name: Annotated[str, typer.Option("--name", help="Device or workload, e.g. alex-laptop.")],
    expires_in: Annotated[
        str, typer.Option("--expires-in", help="Lifetime such as 30d or 12h; required.")
    ],
    secret_output: Annotated[
        Path | None,
        typer.Option(
            "--secret-output",
            callback=invocation_path(),
            help="New file to receive the key (mode 0600); default ~/.harborrag/keys/<name>.key.",
        ),
    ] = None,
    migrate: Annotated[
        bool,
        typer.Option(
            "--migrate",
            help="Apply pending control-plane migrations if the key table is missing.",
        ),
    ] = False,
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Create a reader key for the tenant; the secret goes only to the output file.

    Prints the key's id, tenant, owner, name, expiry and the file holding the secret.
    """

    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    lifetime = parse_duration(expires_in)
    if secret_output is None:
        secret_output = default_secret_output(name)
    check_secret_output(secret_output)
    operator = operator_identity()

    async def call() -> object:
        from harborrag_runtime.security import api_key_operations  # noqa: PLC0415

        settings = _settings()
        if await _guarded(api_key_operations.ensure_key_schema(settings, migrate=migrate)):
            logger.warning("applied the pending control-plane migrations")
        created = await _guarded(
            api_key_operations.create_key(
                settings,
                operator=operator,
                tenant_id=resolved.tenant,
                owner=owner,
                name=name,
                lifetime=lifetime,
            )
        )
        try:
            write_secret_file(secret_output, created.raw_key)
        except OSError:
            await _guarded(
                api_key_operations.revoke_key(
                    settings,
                    operator=operator,
                    key_id=created.key_id,
                    reason="secret delivery failed",
                )
            )
            raise ReportedError(
                ErrorBody(
                    500,
                    "internal_error",
                    "could not write the secret file; the key was revoked, "
                    "fix the path and create a new one",
                    {"key_id": created.key_id},
                )
            ) from None
        return {
            "key_id": created.key_id,
            "tenant_id": created.tenant_id,
            "owner": created.owner,
            "name": created.name,
            "expires_at": created.expires_at.isoformat(),
            "secret_written_to": str(secret_output),
        }

    run_standalone(call, globals=resolved)


@app.command("list")
def list_keys(
    tenant: TenantOption = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """List the tenant's keys with their state; never shows hashes or secrets."""

    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )

    async def call() -> object:
        from harborrag_runtime.security import api_key_operations  # noqa: PLC0415

        settings = _settings()
        await _guarded(
            api_key_operations.ensure_key_schema(settings, migrate=False, require_head=False)
        )
        return {"keys": await _guarded(api_key_operations.list_keys(settings, resolved.tenant))}

    run_standalone(call, globals=resolved)


@app.command("revoke")
def revoke(  # noqa: PLR0913 - the selectors plus the global options
    reason: Annotated[str, typer.Option("--reason", help="Recorded in the audit trail.")],
    key_id: Annotated[
        str | None, typer.Option("--key-id", help="One key; do not combine with --tenant.")
    ] = None,
    owner: Annotated[
        str | None,
        typer.Option("--owner", help="Every active key of one owner; needs an explicit --tenant."),
    ] = None,
    tenant: Annotated[
        str | None,
        typer.Option(
            "--tenant",
            metavar="TENANT_ID",
            help=(
                "Required with --owner, and not allowed with --key-id. Never defaults: "
                f"{TENANT_ENV} and the profile are ignored."
            ),
            rich_help_panel=GLOBAL_OPTIONS_PANEL,
        ),
    ] = None,
    output: OutputOption = None,
    profile: ProfileOption = None,
    config: ConfigOption = None,
    yes: YesOption = False,
) -> None:
    """Revoke one key by id, or all active keys of an owner in a tenant (offboarding).

    Pass --key-id alone, or --owner together with an explicit --tenant.
    """

    by_id = key_id is not None and tenant is None and owner is None
    by_owner = key_id is None and tenant is not None and owner is not None
    if not (by_id or by_owner):
        raise typer.BadParameter(
            "pass either --key-id alone, or --owner together with an explicit --tenant; "
            "revoking never uses the default tenant or HARBORRAG_TENANT_ID"
        )
    resolved = current_globals(
        tenant=tenant, output=output, profile=profile, config=config, yes=yes
    )
    operator = operator_identity()

    async def call() -> object:
        from harborrag_runtime.security import api_key_operations  # noqa: PLC0415

        settings = _settings()
        await _guarded(
            api_key_operations.ensure_key_schema(settings, migrate=False, require_head=False)
        )
        if key_id is not None:
            outcome = await _guarded(
                api_key_operations.revoke_key(
                    settings, operator=operator, key_id=key_id, reason=reason
                )
            )
            if outcome == "not_found":
                raise ReportedError(
                    ErrorBody(
                        404,
                        "not_found",
                        "no key has that id; list the tenant's keys to find it",
                    )
                )
            return {
                "revoked": [key_id] if outcome == "revoked" else [],
                "already_revoked": outcome == "already_revoked",
            }
        if owner is None:  # pragma: no cover - excluded by the selector check
            raise HarborInvariantError("revoke selectors must have been validated")
        revoked = await _guarded(
            api_key_operations.revoke_owner(
                settings,
                operator=operator,
                tenant_id=resolved.tenant,
                owner=owner,
                reason=reason,
            )
        )
        return {"revoked": revoked, "active_keys": len(revoked)}

    run_standalone(call, globals=resolved)
