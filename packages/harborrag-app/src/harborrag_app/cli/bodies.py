"""Request bodies and secrets from a command's files, flags and prompts.

Handles:
- A body file (``-`` for stdin) as JSON or YAML, merged with the flags that set the same
  fields, and validated into the request model; every problem is a usage error (exit 2)
- ``--secret-prompt`` values read without echo

Does NOT call operations or print values: an error names a field and its problem, never the
value it held, so a secret in a rejected body stays out of the terminal.

Start from: load_body, read_fields, read_with_secrets, comma_list.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import click
import yaml
from pydantic import BaseModel, ValidationError

STDIN = "-"


def load_body[M: BaseModel](path: str | None, model: type[M], *, flags: Mapping[str, object]) -> M:
    """Build a request body from a file and the flags that set its fields.

    Args:
        path: Body file; ``-`` reads stdin; ``None`` builds the body from the flags alone.
        model: Request model the body must validate as.
        flags: Field name to the value its flag was given; ``None`` means the flag was not given.

    Returns:
        The validated body.

    Raises:
        click.UsageError: The file cannot be read or parsed, a field is set both by a flag and
            in the file, or the body does not validate.
    """
    return validate_body(model, read_fields(path, flags=flags), source=path or "the flags")


def comma_list(value: str | None) -> list[str] | None:
    """Split a flag's ``a,b`` value into its non-blank parts; ``None`` when it was not given."""
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def read_fields(path: str | None, *, flags: Mapping[str, object]) -> dict[str, object]:
    """Read a body file and add the flags' values, without validating the fields.

    Args:
        path: Body file; ``-`` reads stdin; ``None`` reads nothing.
        flags: Field name to the value its flag was given; ``None`` means not given.

    Returns:
        The fields of the file and the flags.

    Raises:
        click.UsageError: The file cannot be read or parsed, or a field is set twice.
    """
    fields = {} if path is None else read_document(path)
    for field, value in flags.items():
        if value is None:
            continue
        if field in fields:
            raise click.UsageError(
                f"{field} is set both by --{field.replace('_', '-')} and in {path}"
            )
        fields[field] = value
    return fields


def read_document(path: str) -> dict[str, object]:
    """Read a JSON or YAML mapping from a file or, for ``-``, from stdin.

    Raises:
        click.UsageError: The file cannot be read, is not valid JSON or YAML, or holds no mapping.
    """
    try:
        text = sys.stdin.read() if path == STDIN else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise click.UsageError(f"cannot read {path}: {type(exc).__name__}") from None
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise click.UsageError(f"{path} is not valid JSON or YAML{where}") from None
    if not isinstance(document, dict):
        raise click.UsageError(f"{path} must hold one JSON or YAML object")
    return {str(key): value for key, value in document.items()}


def validate_body[M: BaseModel](model: type[M], fields: Mapping[str, object], *, source: str) -> M:
    """Validate ``fields`` as ``model``.

    Raises:
        click.UsageError: Names each invalid field and its problem, never its value.
    """
    try:
        return model.model_validate(fields)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'body'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise click.UsageError(f"invalid body from {source}: {problems}") from None


def read_with_secrets(path: str, secret_fields: Sequence[str]) -> dict[str, object]:
    """Read a connection body file and set the credential fields typed at the prompt.

    Args:
        path: Body file; ``-`` reads stdin, which leaves no terminal to prompt on.
        secret_fields: Credential fields to ask for.

    Returns:
        The file's fields, unchanged when nothing is prompted; else ``credentials`` extended by
        the prompted secrets, created when the file has none.

    Raises:
        click.UsageError: A secret is also in the file, or the prompt cannot be answered.
    """
    document = read_document(path)
    if not secret_fields:
        return document
    credentials = document.get("credentials") or {}
    if not isinstance(credentials, dict):
        raise click.UsageError(f"credentials in {path} must be an object")
    for field in secret_fields:
        if field in credentials:
            raise click.UsageError(
                f"credentials.{field} is set both by --secret-prompt and in {path}"
            )
    return {**document, "credentials": {**credentials, **prompt_secrets(secret_fields)}}


def prompt_secrets(fields: Sequence[str]) -> dict[str, str]:
    """Ask for each secret without echoing it.

    Args:
        fields: Credential field names, one prompt each.

    Returns:
        Field name to the secret typed.

    Raises:
        click.UsageError: stdin is not a terminal, or a secret was left empty.
    """
    if not fields:
        return {}
    if not sys.stdin.isatty():
        raise click.UsageError("--secret-prompt needs a terminal to ask on; stdin is not one")
    secrets: dict[str, str] = {}
    for field in fields:
        value = getpass.getpass(f"{field}: ")
        if not value:
            raise click.UsageError(f"{field} was left empty")
        secrets[field] = value
    return secrets
