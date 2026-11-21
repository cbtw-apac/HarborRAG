"""Clear raw secret values that connection rows still hold.

Migration 0041 split a legacy source's config into ``credentials``, ``config_json`` and
``tuning`` as it found it, so a row written before secret references existed can hold a
token in clear text under a sensitive key. Such a row fails connection validation, and
one of them failed every connection listing.

The values cannot move into the encrypted ``secrets`` table here, because the migration
runner has the database URL but not the secrets encryption key. Each raw value is
removed instead, and the connection turns ``error`` with a ``status_detail`` naming the
fields to set again. A ``deleting`` connection keeps its status so its removal can
finish, and a field holding only null or "" is removed without a status change. The
detection rules are those of ``validate_secret_free_config`` at this revision, frozen
here on purpose. Values are never logged.

Revision ID: 0049
Revises: 0048

Phase: data
Reversible: no-op downgrade; the schema does not change, and a cleared secret is never put
            back in clear text
Touches: sources; only rows with a raw value under a sensitive key, usually none
Locks: row locks on the rewritten sources rows until the migration run commits;
       lock_timeout 5s on Postgres. Offline (--sql) mode emits no data step.
Deploy: before the application release
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

logger = logging.getLogger("harborrag.adapters.control_plane.migrations")

_JSONB = sa.JSON().with_variant(JSONB(), "postgresql")
_TZDT = sa.DateTime(timezone=True)
_SYSTEM_ACTOR = "system"
_DELETING = "deleting"
_ERROR = "error"
_ISSUE = "held a raw value instead of a secret reference and was cleared; set it again"
_FIELD_COLUMNS = (("credentials", "credentials"), ("config_json", "config"), ("tuning", "tuning"))

_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]+")
_SENSITIVE_TOKENS = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "authorization",
        "credential",
        "passwd",
        "password",
        "private_key",
        "secret",
        "token",
    }
)
_COUNT_QUALIFIERS = frozenset(
    {
        "budget",
        "cap",
        "ceiling",
        "chunk",
        "count",
        "estimate",
        "limit",
        "max",
        "min",
        "overlap",
        "per",
        "quota",
        "size",
        "total",
        "usage",
        "window",
    }
)
_CREDENTIAL_LOCATIONS = frozenset({"token_cache_path"})
_SINGLE_WORD_TOKENS = frozenset(token for token in _SENSITIVE_TOKENS if "_" not in token)
_UNSEPARATED_ENTRIES = frozenset(
    {token.replace("_", "") for token in _SENSITIVE_TOKENS if "_" in token}
    | {"secretkey", "authtoken", "passwd", "password", "authorization"}
)
_REFERENCE_KEY = "secret_ref"
_STORED_KEYS = frozenset({_REFERENCE_KEY, "last4", "updated_at"})
_STORED_PREFIX = "secret://"
_REFERENCE_PREFIXES = (_STORED_PREFIX, "env:", "vault:")


def _sources() -> sa.TableClause:
    return sa.table(
        "sources",
        sa.column("id", sa.Text()),
        sa.column("tenant_id", sa.Text()),
        sa.column("status", sa.Text()),
        sa.column("status_detail", _JSONB),
        sa.column("credentials", _JSONB),
        sa.column("config_json", _JSONB),
        sa.column("tuning", _JSONB),
        sa.column("version", sa.Integer()),
        sa.column("updated_by", sa.Text()),
        sa.column("updated_at", _TZDT),
    )


def _is_sensitive(name: object) -> bool:
    """Whether a field name claims to carry a credential."""
    words = _CAMEL_CASE_BOUNDARY.sub("_", str(name).strip())
    canonical = _NON_ALPHANUMERIC.sub("_", words).strip("_").casefold()
    if canonical in _CREDENTIAL_LOCATIONS:
        return False
    if any(entry in canonical.replace("_", "") for entry in _UNSEPARATED_ENTRIES):
        return True
    tokens = {token for token in canonical.split("_") if token}
    matched = {
        entry for entry in _SINGLE_WORD_TOKENS if any(token.endswith(entry) for token in tokens)
    }
    if matched == {"token"} and tokens & _COUNT_QUALIFIERS:
        return False
    return bool(matched)


def _is_reference(value: object) -> bool:
    """Whether a value is a secret reference rather than a raw value."""
    if isinstance(value, str):
        return value.startswith(_STORED_PREFIX)
    return (
        isinstance(value, dict)
        and isinstance(value.get(_REFERENCE_KEY), str)
        and set(value) <= _STORED_KEYS
        and value[_REFERENCE_KEY].startswith(_REFERENCE_PREFIXES)
    )


def _strip(value: object, path: str, removed: dict[str, bool]) -> object:
    """Return ``value`` without raw values under sensitive keys.

    Args:
        value: Stored JSON value, walked through nested objects and lists.
        path: Field path of ``value``, such as ``credentials``.
        removed: Filled with the path of each removed field, and whether its value was set.

    Returns:
        A copy of ``value`` that passes ``validate_secret_free_config``.
    """
    if _is_reference(value):
        return value
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, item in value.items():
            item_path = f"{path}.{key}"
            if _is_sensitive(key) and not _is_reference(item):
                removed[item_path] = item not in (None, "")
            else:
                kept[key] = _strip(item, item_path, removed)
        return kept
    if isinstance(value, list):
        return [_strip(item, f"{path}[{index}]", removed) for index, item in enumerate(value)]
    return value


def _flagged_detail(existing: object, fields: list[str]) -> dict[str, Any]:
    """Add one error per cleared field to the row's ``status_detail``."""
    detail = dict(existing) if isinstance(existing, dict) else {}
    errors = detail.get("errors")
    detail["errors"] = [
        *(errors if isinstance(errors, list) else []),
        *({"field": field, "issue": _ISSUE} for field in fields),
    ]
    return detail


def _clean_row(row: sa.RowMapping) -> tuple[dict[str, Any], dict[str, bool]]:
    """Return the new column values of one row and the fields removed from it."""
    removed: dict[str, bool] = {}
    values: dict[str, Any] = {
        column: _strip(row[column] or {}, field, removed) for column, field in _FIELD_COLUMNS
    }
    cleared = sorted(field for field, was_set in removed.items() if was_set)
    if cleared and row["status"] != _DELETING:
        values["status"] = _ERROR
        values["status_detail"] = _flagged_detail(row["status_detail"], cleared)
    return values, removed


def _bound_lock_wait() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    """Clear raw values under sensitive connection fields and flag the connections."""
    _bound_lock_wait()
    if op.get_context().as_sql:
        op.execute("-- 0049 clears raw connection secrets only when run against a database")
        return
    bind = op.get_bind()
    sources = _sources()
    rows = bind.execute(
        sa.select(
            sources.c.id,
            sources.c.tenant_id,
            sources.c.status,
            sources.c.status_detail,
            *(sources.c[column] for column, _field in _FIELD_COLUMNS),
        ).order_by(sources.c.id)
    ).mappings()
    now = datetime.now(UTC)
    rewritten = 0
    for row in list(rows):
        values, removed = _clean_row(row)
        if not removed:
            continue
        bind.execute(
            sa.update(sources)
            .where(sources.c.id == row["id"])
            .values(
                **values,
                version=sources.c.version + 1,
                updated_by=_SYSTEM_ACTOR,
                updated_at=now,
            )
        )
        rewritten += 1
        logger.warning(
            "Cleared raw connection secrets connection_pk=%s tenant_id=%s fields=%s",
            row["id"],
            row["tenant_id"],
            ",".join(sorted(removed)),
        )
    if rewritten:
        logger.warning("Cleared raw connection secrets connections=%d", rewritten)


def downgrade() -> None:
    """Leave cleared values cleared; the schema is unchanged and secrets stay out of rows."""
