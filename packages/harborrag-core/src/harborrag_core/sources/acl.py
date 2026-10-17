"""Source-side access evidence a connector captures for one item.

A connector reports what its source says about who may read an item, relative to
the scope it was configured to read. Everything here is immutable evidence, never
a grant: effective decisions are made elsewhere, from this evidence plus a
directory of the source's own identities.

Two coordinates keep the evidence unambiguous. ``realm`` names the identity
namespace the principals live in (``atlassian:<host>``, ``entra:<tenant>``), so a
group id from one tenant can never be mistaken for the same id in another.
``scope`` names the readable unit the ACL is relative to (``jira-project:<KEY>``,
``confluence-space:<KEY>``, ``sharepoint-drive:<id>``): ``inherited`` means "exactly
the readers of that scope".

Decoding fails closed. A payload that omits ``realm`` or ``scope``, or that is
malformed in any other way, raises :class:`ValueError`; it is never read as
``inherited``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

AclStatus = Literal["inherited", "restricted", "unknown"]
SOURCE_ACL_METADATA_KEY = "source_acl"

_STATUSES: frozenset[str] = frozenset(get_args(AclStatus))


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_str(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")


@dataclass(frozen=True, slots=True, order=True, kw_only=True)
class SourcePrincipal:
    """One source-side identity named by an ACL, e.g. a Jira group or an Entra user.

    Identity is ``(realm, kind, id)``. ``display_name`` is a label for people and
    never takes part in equality, hashing or ordering. A realm may contain ``:``
    but not ``|``, and a kind contains neither, so that :attr:`key` names exactly
    one principal; ids are unrestricted.
    """

    realm: str
    kind: str
    id: str
    display_name: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.realm, "source principal realm")
        _require_text(self.kind, "source principal kind")
        _require_text(self.id, "source principal id")
        # ``key`` is ``<realm>|<kind>:<id>``. It is injective, so one key names one
        # principal, because it is parsed left to right: the realm ends at the first
        # ``|`` and the kind at the next ``:``. Ids stay free-form (they are parsed last).
        if "|" in self.realm:
            raise ValueError("source principal realm must not contain '|'")
        if "|" in self.kind or ":" in self.kind:
            raise ValueError("source principal kind must not contain '|' or ':'")
        if self.display_name is not None:
            _require_str(self.display_name, "source principal display_name")

    @property
    def key(self) -> str:
        """Stable directory key, ``<realm>|<kind>:<id>``."""

        return f"{self.realm}|{self.kind}:{self.id}"

    def to_dict(self) -> dict[str, str]:
        payload = {"realm": self.realm, "kind": self.kind, "id": self.id}
        if self.display_name:
            payload["display_name"] = self.display_name
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourcePrincipal:
        """Decode :meth:`to_dict`; any missing or blank identity field raises ``ValueError``."""

        if not isinstance(payload, Mapping):
            raise ValueError("a source principal must be a mapping")
        return cls(
            realm=payload.get("realm"),  # type: ignore[arg-type]
            kind=payload.get("kind"),  # type: ignore[arg-type]
            id=payload.get("id"),  # type: ignore[arg-type]
            display_name=payload.get("display_name"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceAcl:
    """Read access for one source item, relative to the connector's source scope.

    ``inherited``: readable by exactly the readers of ``scope``.
    ``restricted``: narrower; a reader must match at least one principal in
    *every* level (Confluence page and ancestor restrictions intersect).
    ``unknown``: access could not be determined; always handled fail-closed.

    ``realm`` and ``scope`` are required unless the status is ``unknown``, where
    the connector may have failed before it could name either.
    """

    realm: str
    scope: str
    status: AclStatus
    levels: tuple[tuple[SourcePrincipal, ...], ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in _STATUSES:
            raise ValueError(f"unsupported source ACL status: {self.status!r}")
        _require_str(self.realm, "source ACL realm")
        _require_str(self.scope, "source ACL scope")
        _require_str(self.reason, "source ACL reason")
        if self.status != "unknown":
            _require_text(self.realm, "source ACL realm")
            _require_text(self.scope, "source ACL scope")
        levels = self._frozen_levels()
        object.__setattr__(self, "levels", levels)
        if self.status == "restricted" and not levels:
            raise ValueError("restricted source ACLs need at least one principal level")
        if self.status != "restricted" and levels:
            raise ValueError(f"{self.status} source ACLs carry no principal levels")
        if any(not level for level in levels):
            raise ValueError("source ACL principal levels must be non-empty")
        for level in levels:
            for principal in level:
                if principal.realm != self.realm:
                    raise ValueError(
                        f"source ACL principal {principal.key!r} is outside "
                        f"the ACL realm {self.realm!r}"
                    )

    def _frozen_levels(self) -> tuple[tuple[SourcePrincipal, ...], ...]:
        try:
            levels = tuple(tuple(level) for level in self.levels)
        except TypeError:
            raise ValueError("source ACL levels must be sequences of principals") from None
        for level in levels:
            if not all(isinstance(principal, SourcePrincipal) for principal in level):
                raise ValueError("source ACL levels must contain only source principals")
        return levels

    @classmethod
    def inherited(cls, *, realm: str, scope: str) -> SourceAcl:
        return cls(realm=realm, scope=scope, status="inherited")

    @classmethod
    def restricted(
        cls,
        *,
        realm: str,
        scope: str,
        levels: Iterable[Iterable[SourcePrincipal]],
        reason: str,
    ) -> SourceAcl:
        return cls(
            realm=realm,
            scope=scope,
            status="restricted",
            levels=tuple(tuple(level) for level in levels),
            reason=reason,
        )

    @classmethod
    def unknown(cls, *, reason: str, realm: str = "", scope: str = "") -> SourceAcl:
        return cls(realm=realm, scope=scope, status="unknown", reason=reason)

    def canonical(self) -> SourceAcl:
        """The equivalent ACL in the one form that names its access class.

        A reader must match a principal in every level, so neither the order of
        levels nor a repeated level or principal changes who may read; the
        free-text ``reason`` never does. The canonical ACL keeps ``realm``,
        ``scope`` and ``status``, sorts each level's principals by :attr:`SourcePrincipal.key`
        without duplicates (the first of a repeated key is kept, display name and
        all), sorts the levels by their keys without duplicates, and clears
        ``reason``. ``[[a, b]]`` ("a or b") and ``[[a], [b]]`` ("a and b") stay apart.

        An ``unknown`` ACL has no access to name: it is returned unchanged.
        """

        if self.status == "unknown":
            return self
        levels: dict[tuple[str, ...], tuple[SourcePrincipal, ...]] = {}
        for level in self.levels:
            principals: dict[str, SourcePrincipal] = {}
            for principal in level:
                principals.setdefault(principal.key, principal)
            keys = tuple(sorted(principals))
            levels.setdefault(keys, tuple(principals[key] for key in keys))
        return SourceAcl(
            realm=self.realm,
            scope=self.scope,
            status=self.status,
            levels=tuple(levels[keys] for keys in sorted(levels)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "realm": self.realm,
            "scope": self.scope,
            "status": self.status,
            "reason": self.reason,
            "levels": [[principal.to_dict() for principal in level] for level in self.levels],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourceAcl:
        """Decode :meth:`to_dict`; a missing or blank required field raises ``ValueError``.

        ``realm`` and ``scope`` must be present for every status, since
        :meth:`to_dict` always writes them (blank only for ``unknown``). Payloads
        stored before ACLs carried them are therefore rejected, never guessed at.
        """

        if not isinstance(payload, Mapping):
            raise ValueError("a source ACL must be a mapping")
        status = payload.get("status")
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError(f"unsupported source ACL status: {status!r}")
        return cls(
            realm=_decode_text(payload, "realm", required=True),
            scope=_decode_text(payload, "scope", required=True),
            status=cast(AclStatus, status),
            levels=_decode_levels(payload.get("levels")),
            reason=_decode_text(payload, "reason", required=False),
        )


def _decode_text(payload: Mapping[str, Any], name: str, *, required: bool) -> str:
    value = payload.get(name)
    if value is None:
        if required:
            raise ValueError(f"source ACL {name} is required")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"source ACL {name} must be a string")
    return value


def _decode_levels(raw: object) -> tuple[tuple[SourcePrincipal, ...], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("source ACL levels must be a list of principal lists")
    levels: list[tuple[SourcePrincipal, ...]] = []
    for level in raw:
        if not isinstance(level, (list, tuple)):
            raise ValueError("each source ACL level must be a list of principals")
        levels.append(tuple(SourcePrincipal.from_dict(item) for item in level))
    return tuple(levels)


def acl_metadata(acl: SourceAcl) -> dict[str, Any]:
    """Metadata a record or document carries for its ACL; inherited ACLs are recorded too."""

    return {SOURCE_ACL_METADATA_KEY: acl.to_dict()}


def acl_from_metadata(metadata: Mapping[str, Any]) -> SourceAcl | None:
    """Read the ACL from metadata: ``None`` when absent, ``ValueError`` when malformed."""

    if SOURCE_ACL_METADATA_KEY not in metadata:
        return None
    return SourceAcl.from_dict(metadata[SOURCE_ACL_METADATA_KEY])
