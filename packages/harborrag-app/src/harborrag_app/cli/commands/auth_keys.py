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
from datetime import UTC, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

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
    # Windows reports a synthetic st_mode (directories look 0o777), so the POSIX
    # world-writable test is meaningless there; access is governed by NTFS ACLs.
    if os.name != "nt" and parent.stat().st_mode & stat.S_IWOTH:
        raise typer.BadParameter(f"{parent} is world-writable; use a private directory")


def write_secret_file(path: Path, secret: str) -> None:
    # O_EXCL: fail if the file appeared meanwhile. 0600: owner-only from the start.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(secret + "\n")


MANAGEMENT_DSN_FILE_ENV = "HARBORRAG_MANAGEMENT_DATABASE_DSN_FILE"


def _fail(error_code: str, detail: str | None = None) -> NoReturn:
    payload: dict[str, Any] = {"ok": False, "error_code": error_code}
    if detail:
        payload["detail"] = detail
    typer.echo(json.dumps(payload), err=True)
    raise typer.Exit(1)


def read_management_dsn() -> str:
    """The management DSN lives in a file so it never appears in argv or shell history."""

    location = os.environ.get(MANAGEMENT_DSN_FILE_ENV, "").strip()
    if not location:
        _fail("ManagementDsnFileNotSet", f"set {MANAGEMENT_DSN_FILE_ENV} to a file holding the DSN")
    path = Path(location).expanduser()
    try:
        if os.name != "nt" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            _fail("ManagementDsnFileNotPrivate", "restrict the file to mode 0600")
        dsn = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        _fail("ManagementDsnFileUnreadable", type(error).__name__)  # type only: no path or value
    if not dsn:
        _fail("ManagementDsnEmpty", "the management DSN file is empty")
    return dsn


def _run(operation: Coroutine[Any, Any, Any]) -> Any:
    from harborrag_engine.security import ApiKeyPolicyError
    from harborrag_runtime.security.api_key_operations import ApiKeySchemaMissing

    try:
        return asyncio.run(operation)
    except ApiKeyPolicyError as error:
        # Authored in-repo and never echoes input, so the operator gets the rule.
        # The type tells TenantNotFound apart from the other policy rejections.
        typer.echo(
            json.dumps({"ok": False, "error_code": type(error).__name__, "detail": str(error)}),
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

    dsn = read_management_dsn()
    try:
        overrides = settings_overrides(find_legacy_checkout())
        overrides["control_db_url"] = dsn  # wins over HARBORRAG_CONTROL_DB_URL and the checkout
        return RuntimeSettings(**overrides)
    except Exception as error:
        # A settings error would otherwise print the model, values included.
        _fail(type(error).__name__)


_LIST_COLUMNS = (
    ("KEY ID", "key_id"),
    ("NAME", "name"),
    ("OWNER", "owner"),
    ("STATE", "state"),
    ("EXPIRES", "expires_at"),
)


def render_key_table(keys: list[dict[str, Any]]) -> str:
    """Only the five operator-facing columns, so a hash or secret cannot leak into the table."""

    header = [title for title, _ in _LIST_COLUMNS]
    rows = [[str(key[field]) for _, field in _LIST_COLUMNS] for key in keys]
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=True)]
    return "\n".join(
        " | ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in (header, *rows)
    )


def render_created_key(created: Any, secret_output: Path) -> str:
    """Operator-facing summary; the secret itself never appears, only where it was written."""

    expires = created.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "\n".join(
        (
            "Credential created.",
            f"Key ID:   {created.key_id}",
            f"Tenant:   {created.tenant_id}",
            f"Owner:    {created.owner}",
            f"Name:     {created.name}",
            f"Expires:  {expires}",
            f"Secret written to {secret_output} (mode 0600)",
        )
    )


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
    settings = _settings()  # refuse to start before touching the filesystem
    if secret_output is None:
        secret_output = default_secret_output(name)
    check_secret_output(secret_output)
    operator = operator_identity()
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
    typer.echo(render_created_key(created, secret_output))


@app.command("list")
def list_keys(tenant: Tenant) -> None:
    """List a tenant's keys with their state; never shows hashes or secrets."""

    from harborrag_runtime.security import api_key_operations

    operator = operator_identity()
    settings = _settings()
    _run(api_key_operations.ensure_key_schema(settings, migrate=False))
    keys = _run(api_key_operations.list_keys(settings, operator=operator, tenant_id=tenant))
    typer.echo(render_key_table(keys) if keys else f"no keys for tenant {tenant}")


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
        typer.echo(f"revoked {key_id}" if changed else "already revoked")
        return
    if tenant is None or owner is None:  # pragma: no cover - excluded by the selector check
        raise HarborInvariantError("revoke selectors must have been validated")
    revoked = _run(
        api_key_operations.revoke_owner(
            settings, operator=operator, tenant_id=tenant, owner=owner, reason=reason
        )
    )
    typer.echo("\n".join(f"revoked {revoked_id}" for revoked_id in revoked) or "already revoked")
