"""Strict parsing of one stored `providers` row into a catalog deployment.

Nothing writes these rows yet, so this module *defines* the accepted shape
rather than tolerating a historical one: it is deliberately narrow, and an
unknown key is an error naming the offending row rather than a silently
ignored field that the operator believes took effect.

`providers.family` is the model family (`chat`/`embedding`/`reranker`), so
the vendor lives in `config_json["provider"]` and must be one of the families
`harborrag_adapters.models.chat.registry` can actually build a client for.
The API key never appears here -- only the row's `secret_ref` column.

Accepted `providers.config_json` for a `family = "chat"` row::

    {
      "provider": "openai",              # required; a HarborProvider value
      "logical_model": "fast",           # required; the tenant-facing name
      "model": "gpt-4o-mini",            # required; the vendor's model id
      "deployment": "fast-eu",           # optional; defaults to the row's name
      "api_base": "https://...",         # optional; absolute URL, unchecked here
      "capabilities": {"streaming": true},  # optional; str -> bool only
      "weight": 2,                       # optional; positive int, default 1
      "default": true,                   # optional; marks the tenant default
      "extra": {"organization": "acme"}  # optional; free-form passthrough
    }

Several rows sharing a `logical_model` become several deployments behind that
one name; their `deployment` values must differ.

Accepted `routing_rules.rule_json` for a `family = "chat"` rule::

    {"weight": 3, "strategy": "weighted"}

Both keys are optional. `weight` overrides the deployment's configured
weight; `strategy` is surfaced as `extra["routing_strategy"]`. A malformed
rule is dropped on its own -- the provider row it points at survives with its
configured weight, because a bad routing tweak must not take chat offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from harborrag_core.ports.model_catalog import TenantModelDeployment

CHAT_FAMILY = "chat"

_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "provider",
        "logical_model",
        "model",
        "deployment",
        "api_base",
        "capabilities",
        "weight",
        "default",
        "extra",
    }
)
_ALLOWED_RULE_KEYS = frozenset({"weight", "strategy"})


class ProviderRowError(ValueError):
    """One stored row is unusable; the rest of the tenant is unaffected."""


@lru_cache(maxsize=1)
def known_provider_families() -> frozenset[str]:
    """Return the vendor families the chat registry can build a client for.

    Imported lazily: this module sits in the repository layer and must not
    drag the LiteLLM-backed model stack into every control-plane import.
    """
    from harborrag_adapters.models.chat.registry import HarborProvider

    return frozenset(str(provider) for provider in HarborProvider)


@dataclass(frozen=True, slots=True)
class RuleOverride:
    """A validated `rule_json` body: what it changes about one deployment."""

    weight: int | None = None
    strategy: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRowInput:
    """One `providers` row plus the validated rule that points at it, if any."""

    row_id: str
    name: str
    config: Mapping[str, object]
    secret_ref: str | None = None
    rule: RuleOverride | None = None


@dataclass(frozen=True, slots=True)
class ParsedProviderRow:
    """A validated row: which logical model it serves, and how."""

    row_id: str
    logical_model: str
    deployment: TenantModelDeployment
    is_default: bool


def _reject_unknown(config: Mapping[str, object], *, row_id: str, label: str) -> None:
    allowed = _ALLOWED_CONFIG_KEYS if label == "config_json" else _ALLOWED_RULE_KEYS
    unknown = sorted(key for key in config if key not in allowed)
    if unknown:
        raise ProviderRowError(
            f"provider row {row_id!r} has unknown {label} key(s): {', '.join(unknown)}"
        )


def _text(config: Mapping[str, object], key: str, *, row_id: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderRowError(
            f"provider row {row_id!r} needs a non-blank string {key!r}, got {value!r}"
        )
    return value


def _optional_text(config: Mapping[str, object], key: str, *, row_id: str) -> str | None:
    if key not in config or config[key] is None:
        return None
    return _text(config, key, row_id=row_id)


def _weight(value: object, *, row_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProviderRowError(
            f"provider row {row_id!r} needs a positive int weight, got {value!r}"
        )
    return value


def _capabilities(value: object, *, row_id: str) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        raise ProviderRowError(f"provider row {row_id!r} capabilities must be an object")
    for key, flag in value.items():
        if not isinstance(key, str) or not isinstance(flag, bool):
            raise ProviderRowError(
                f"provider row {row_id!r} capabilities must map strings to booleans, "
                f"got {key!r}: {flag!r}"
            )
    return dict(value)


def _extra(value: object, *, row_id: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ProviderRowError(f"provider row {row_id!r} extra must be a string-keyed object")
    return dict(value)


def _provider(config: Mapping[str, object], *, row_id: str) -> str:
    provider = _text(config, "provider", row_id=row_id)
    known = known_provider_families()
    if provider not in known:
        raise ProviderRowError(
            f"provider row {row_id!r} names unsupported provider {provider!r}; "
            f"supported: {', '.join(sorted(known))}"
        )
    return provider


def parse_rule(rule: Mapping[str, object], *, row_id: str) -> RuleOverride:
    """Validate a `rule_json` body into an optional weight override and strategy.

    Raises ProviderRowError naming the rule row; the caller drops just the
    rule so the provider row it points at survives with its own weight.
    """
    if not isinstance(rule, Mapping):
        raise ProviderRowError(f"provider row {row_id!r} rule_json must be an object")
    _reject_unknown(rule, row_id=row_id, label="rule_json")
    return RuleOverride(
        weight=None if rule.get("weight") is None else _weight(rule["weight"], row_id=row_id),
        strategy=_optional_text(rule, "strategy", row_id=row_id),
    )


def parse_provider_row(candidate: ProviderRowInput) -> ParsedProviderRow:
    """Validate one stored provider row, raising ProviderRowError naming the row."""
    row_id = candidate.row_id
    config = candidate.config
    if not isinstance(config, Mapping):
        raise ProviderRowError(f"provider row {row_id!r} config_json must be an object")
    _reject_unknown(config, row_id=row_id, label="config_json")

    weight = 1 if config.get("weight") is None else _weight(config["weight"], row_id=row_id)
    extra = _extra(config.get("extra", {}), row_id=row_id)
    rule = candidate.rule
    if rule is not None:
        weight = weight if rule.weight is None else rule.weight
        if rule.strategy is not None:
            extra["routing_strategy"] = rule.strategy

    default = config.get("default", False)
    if not isinstance(default, bool):
        raise ProviderRowError(f"provider row {row_id!r} default must be a boolean")

    deployment = TenantModelDeployment(
        name=_optional_text(config, "deployment", row_id=row_id) or candidate.name,
        provider=_provider(config, row_id=row_id),
        model=_text(config, "model", row_id=row_id),
        secret_ref=candidate.secret_ref,
        api_base=_optional_text(config, "api_base", row_id=row_id),
        capabilities=_capabilities(config.get("capabilities", {}), row_id=row_id),
        weight=weight,
        extra=extra,
    )
    return ParsedProviderRow(
        row_id=row_id,
        logical_model=_text(config, "logical_model", row_id=row_id),
        deployment=deployment,
        is_default=default,
    )
