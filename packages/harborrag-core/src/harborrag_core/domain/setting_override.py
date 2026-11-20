"""Setting override aggregate: one key's value stored at one scope within a tenant."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from harborrag_core.base import utc_now
from harborrag_core.domain.setting_registry import ScopeLevel, get_setting_spec

from .validation import require_id, require_tenant_id

SYSTEM_ACTOR = "system"


@dataclass(slots=True)
class SettingOverride:
    """One overridden setting value at one scope, validated against `SETTING_REGISTRY`."""

    tenant_id: str
    scope_level: ScopeLevel
    scope_id: str
    key: str
    value: Any
    version: int = 1
    created_by: str = SYSTEM_ACTOR
    updated_by: str = SYSTEM_ACTOR
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        """Validate identity fields and the value against the key's registered spec.

        Raises:
            KeyError: ``key`` is not in `SETTING_REGISTRY`.
            ValueError: ``scope_level`` is not allowed for ``key``, or ``value`` fails
                the key's type/range/enum checks.
        """
        require_tenant_id(self.tenant_id)
        require_id(self.scope_id, label="Scope")
        spec = get_setting_spec(self.key)
        if self.scope_level not in spec.levels:
            raise ValueError(f"{self.key} cannot be overridden at level {self.scope_level!r}")
        spec.validate_value(self.value)
