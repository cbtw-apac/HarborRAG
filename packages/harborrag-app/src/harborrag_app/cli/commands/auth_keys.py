"""Issue, list and revoke MCP reader keys.

The secret is written once to a file the operator names, created with
``O_EXCL`` and mode 0600, and never printed: terminal scrollback, shell
recordings and CI logs all outlive a key's usefulness.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import socket
import stat
from collections.abc import Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from harborrag_core.invariants import HarborInvariantError

app = typer.Typer(no_args_is_help=True, help="Hashed, tenant-bound MCP reader keys with a TTL.")

Tenant = Annotated[str, typer.Option("--tenant", help="Tenant the key may read.")]
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


def _run(operation: Coroutine[Any, Any, Any]) -> Any:
    from harborrag_engine.security import ApiKeyPolicyError
    from harborrag_runtime.security.api_key_operations import ApiKeySchemaMissing

    try:
        return asyncio.run(operation)
    except ApiKeyPolicyError as error:
        # Authored in-repo and never echoes input, so the operator gets the rule.
        typer.echo(
            json.dumps({"ok": False, "error_code": "ApiKeyPolicyError", "detail": str(error)}),
            err=True,
        )
        raise typer.Exit(1) from None
    except ApiKeySchemaMissing as error:
        # Authored in-repo, names no values: the operator needs the whole hint.
        typer.echo(
            json.dumps({"ok": False, "error_code": "SchemaMissing", "detail": str(error)}), err=True
        )
        raise typer.Exit(1) from None
    except Exception as error:
        # The type alone: a message could carry a DSN or a policy detail with input.
        typer.echo(json.dumps({"ok": False, "error_code": type(error).__name__}), err=True)
        raise typer.Exit(1) from None


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
    """Runtime settings for the control database, under the JSON error envelope."""

    from harborrag_app.cli.project import find_legacy_checkout
    from harborrag_runtime.config.settings import RuntimeSettings

    try:
        settings = RuntimeSettings(**settings_overrides(find_legacy_checkout()))
    except Exception as error:
        # A settings error would otherwise print the model, values included.
        typer.echo(json.dumps({"ok": False, "error_code": type(error).__name__}), err=True)
        raise typer.Exit(1) from None
    if settings.control_db_url.get_secret_value().lower().startswith("sqlite"):
        # Almost always a mistake: no MCP server reads a SQLite file from the
        # operator's working directory. Say so, but let a deliberate local setup proceed.
        typer.echo(
            json.dumps(
                {
                    "warning": "control database is the local SQLite default; run from the "
                    "checkout root (env/.env.database) or export HARBORRAG_CONTROL_DB_URL "
                    "so keys land in the database the MCP server reads"
                }
            ),
            err=True,
        )
    return settings


def _emit(data: dict[str, Any]) -> None:
    typer.echo(json.dumps({"ok": True, "data": data}, default=str))


@app.command("create")
def create(  # noqa: PLR0913 - one parameter per documented option
    tenant: Tenant,
    owner: Annotated[str, typer.Option("--owner", help="user-<name>, svc-<name> or team-<name>.")],
    name: Annotated[str, typer.Option("--name", help="Device or workload, e.g. huy-laptop.")],
    expires_in: Annotated[
        str, typer.Option("--expires-in", help="Lifetime such as 30d or 12h; required.")
    ],
    secret_output: Annotated[
        Path | None,
        typer.Option(
            "--secret-output",
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
) -> None:
    """Create a reader key; the secret goes only to the output file, never to the terminal."""

    from harborrag_runtime.security import api_key_operations

    lifetime = parse_duration(expires_in)
    if secret_output is None:
        secret_output = default_secret_output(name)
    check_secret_output(secret_output)
    operator = operator_identity()
    settings = _settings()
    if _run(api_key_operations.ensure_key_schema(settings, migrate=migrate)):
        typer.echo(json.dumps({"ok": True, "data": {"migrated": True}}), err=True)
    created = _run(
        api_key_operations.create_key(
            settings,
            operator=operator,
            tenant_id=tenant,
            owner=owner,
            name=name,
            lifetime=lifetime,
        )
    )
    try:
        write_secret_file(secret_output, created.raw_key)
    except OSError:
        # A key nobody can collect is a liability, not an asset: withdraw it now.
        _run(
            api_key_operations.revoke_key(
                settings,
                operator=operator,
                key_id=created.key_id,
                reason="secret delivery failed",
            )
        )
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "SecretDeliveryFailed",
                    "key_id": created.key_id,
                    "detail": "could not write the secret file; the key was revoked, "
                    "fix the path and create a new one",
                }
            ),
            err=True,
        )
        raise typer.Exit(1) from None
    _emit(
        {
            "key_id": created.key_id,
            "tenant_id": created.tenant_id,
            "owner": created.owner,
            "name": created.name,
            "expires_at": created.expires_at.isoformat(),
            "secret_written_to": str(secret_output),
        }
    )


@app.command("list")
def list_keys(tenant: Tenant) -> None:
    """List a tenant's keys with their state; never shows hashes or secrets."""

    from harborrag_runtime.security import api_key_operations

    settings = _settings()
    _run(api_key_operations.ensure_key_schema(settings, migrate=False))
    _emit({"keys": _run(api_key_operations.list_keys(settings, tenant))})


@app.command("revoke")
def revoke(
    reason: Annotated[str, typer.Option("--reason", help="Recorded in the audit trail.")],
    key_id: Annotated[str | None, typer.Option("--key-id", help="One key.")] = None,
    tenant: Annotated[str | None, typer.Option("--tenant")] = None,
    owner: Annotated[
        str | None, typer.Option("--owner", help="With --tenant: every active key of one owner.")
    ] = None,
) -> None:
    """Revoke one key by id, or all active keys of an owner in a tenant (offboarding)."""

    from harborrag_runtime.security import api_key_operations

    by_id = key_id is not None and tenant is None and owner is None
    by_owner = key_id is None and tenant is not None and owner is not None
    if not (by_id or by_owner):
        raise typer.BadParameter("pass either --key-id, or --tenant together with --owner")
    operator = operator_identity()
    settings = _settings()
    _run(api_key_operations.ensure_key_schema(settings, migrate=False))
    if key_id is not None:
        changed = _run(
            api_key_operations.revoke_key(settings, operator=operator, key_id=key_id, reason=reason)
        )
        _emit({"revoked": [key_id] if changed else [], "already_revoked": not changed})
        return
    if tenant is None or owner is None:  # pragma: no cover - excluded by the selector check
        raise HarborInvariantError("revoke selectors must have been validated")
    revoked = _run(
        api_key_operations.revoke_owner(
            settings, operator=operator, tenant_id=tenant, owner=owner, reason=reason
        )
    )
    _emit({"revoked": revoked, "already_revoked": not revoked})
