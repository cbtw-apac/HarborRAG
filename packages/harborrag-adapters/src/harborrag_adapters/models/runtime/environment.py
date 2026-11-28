from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from typing import Any

_ENVIRONMENT_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_REQUIRED_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ANY_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def expand_environment(value: Any) -> Any:
    """Recursively expand ``${VAR}`` and ``${VAR:-default}`` references."""

    if isinstance(value, str):
        full_match = _ENVIRONMENT_REFERENCE.fullmatch(value)
        if full_match:
            return _environment_value(*full_match.groups())
        return _ENVIRONMENT_REFERENCE.sub(
            lambda match: _environment_value(*match.groups()),
            value,
        )
    if isinstance(value, Mapping):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if isinstance(value, tuple):
        return tuple(expand_environment(item) for item in value)
    return value


def require_set_variables(value: object) -> None:
    """Refuse a ``${VAR}`` reference in parsed configuration whose variable is blank.

    ``expand_environment`` accepts an empty variable, which then surfaces only as an
    authentication failure on the first call. A ``${VAR:-default}`` reference is
    skipped, since its default decides. Pass parsed values, not raw text, so a
    reference in a YAML comment never counts.

    Args:
        value: Parsed configuration of strings, mappings and lists.

    Raises:
        ValueError: ``NAME is blank``, naming every blank variable in sorted order.
    """
    blank = sorted(
        {
            name
            for text in _string_values(value)
            for name in _REQUIRED_REFERENCE.findall(text)
            if not os.environ.get(name, "").strip()
        }
    )
    if blank:
        raise ValueError(f"{', '.join(blank)} is blank")


def catalog_has_rerank_model(document: Mapping[str, Any]) -> bool:
    """Return whether a model catalog's ``rerank`` section names a model.

    A ``rerank`` section whose deployments all expand to an empty ``model`` is an
    optional reranker left off, such as ``model: ${HARBORRAG_RERANKER_MODEL:-}`` with
    the variable unset. Any other ``rerank`` section counts, whatever it holds, so the
    loader reports what is wrong with it.

    Args:
        document: A catalog read by ``load_config_document``.

    Returns:
        Whether a ``rerank`` section is present and names at least one model.

    Raises:
        ValueError: A named rerank model's ``api_key`` reads a variable that is
            empty, such as ``HARBORRAG_RERANKER_API_KEY``. Every provider refuses it,
            because LiteLLM would send an ambient key such as ``COHERE_API_KEY``
            instead. The message names the variable, never a value. A ``model`` that
            reads an unset variable with no default is refused too.
    """
    if "rerank" not in document:
        return False
    deployments = _rerank_deployments(document["rerank"])
    if deployments is None:
        return True
    named = [item for item in deployments if str(expand_environment(item["model"])).strip()]
    for deployment in named:
        _require_rerank_key(deployment)
    return bool(named)


def _environment_value(name: str, default: str | None) -> str:
    resolved = os.getenv(name, default)
    if resolved is None:
        raise ValueError(f"environment variable {name} is not set")
    return resolved


def _string_values(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _string_values(item)


def _rerank_deployments(section: object) -> list[Mapping[str, Any]] | None:
    """Return the rerank deployments that name a ``model``, or None for another shape."""
    definitions = section.get("models") if isinstance(section, Mapping) else None
    if not isinstance(definitions, Mapping) or not definitions:
        return None
    found: list[Mapping[str, Any]] = []
    for definition in definitions.values():
        deployments = definition.get("deployments") if isinstance(definition, Mapping) else None
        if not isinstance(deployments, list) or not deployments:
            return None
        for deployment in deployments:
            if not isinstance(deployment, Mapping) or "model" not in deployment:
                return None
            found.append(deployment)
    return found


def _require_rerank_key(deployment: Mapping[str, Any]) -> None:
    """Refuse an ``api_key`` whose variables expand to blank, naming the variables."""
    key = deployment.get("api_key")
    variables = _ANY_REFERENCE.findall(key) if isinstance(key, str) else []
    if variables and not str(expand_environment(key)).strip():
        raise ValueError(f"{', '.join(variables)} is empty, and the rerank model needs it")
