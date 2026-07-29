"""Command-line interface for validating and inspecting model configuration files."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import yaml

from .chat.configs import HarborChatClientConfig
from .embed.configs import HarborEmbedClientConfig
from .rerank.configs import HarborRerankClientConfig

type SupportedConfig = HarborChatClientConfig | HarborEmbedClientConfig | HarborRerankClientConfig

# Returns the text to write to stdout, or None when the command wrote a file itself.
type CommandHandler = Callable[[SupportedConfig, argparse.Namespace], str | None]

_CONFIG_TYPES: Mapping[str, type[SupportedConfig]] = MappingProxyType(
    {
        "chat": HarborChatClientConfig,
        "embed": HarborEmbedClientConfig,
        "rerank": HarborRerankClientConfig,
    }
)

# Pydantic's ValidationError subclasses ValueError, so these cover a malformed
# document, a schema violation, and an unreadable path alike.
_CONFIG_ERRORS = (OSError, ValueError, KeyError, yaml.YAMLError)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Harbor model configuration command-line interface."""

    args = _parser().parse_args(argv)
    try:
        config = _load_config(args.family, args.file, args.profile)
    except _CONFIG_ERRORS as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        output = _COMMANDS[args.command](config, args)
    except OSError as exc:
        print(f"output error: {exc}", file=sys.stderr)
        return 3

    if output is not None:
        print(output, end="" if output.endswith("\n") else "\n")
    return 0


def _validate(config: SupportedConfig, args: argparse.Namespace) -> str:
    return f"valid {args.family} configuration: {args.file}"


def _render(config: SupportedConfig, args: argparse.Namespace) -> str | None:
    text = _rendered_text(config, args.format)
    if args.output is None:
        return text
    output_path = cast(Path, args.output)
    output_path.write_text(text, encoding="utf-8")
    return None


def _explain(config: SupportedConfig, args: argparse.Namespace) -> str:
    return json.dumps(_explanation(config), indent=2, sort_keys=True)


_COMMANDS: Mapping[str, CommandHandler] = MappingProxyType(
    {
        "validate": _validate,
        "render": _render,
        "explain": _explain,
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harborrag-models")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in _COMMANDS:
        command = subcommands.add_parser(name)
        command.add_argument("file", type=Path)
        command.add_argument("--family", choices=tuple(_CONFIG_TYPES), required=True)
        command.add_argument("--profile")
        if name == "render":
            command.add_argument("--format", choices=("json", "yaml"), default="yaml")
            command.add_argument("--output", type=Path)
    return parser


def _load_config(family: str, path: Path, profile: str | None) -> SupportedConfig:
    return _CONFIG_TYPES[family].from_file(path, profile=profile)


def _rendered_text(config: SupportedConfig, format_name: str) -> str:
    rendered = config.sanitized_dump()
    if format_name == "json":
        return json.dumps(rendered, indent=2, sort_keys=True)
    return yaml.safe_dump(rendered, sort_keys=True)


def _explanation(config: SupportedConfig) -> dict[str, Any]:
    models = cast(dict[str, Any], config.models)
    return {
        "family": config.config_section,
        "default_model": config.default_model,
        "logical_models": {
            logical: _logical_model_summary(model) for logical, model in sorted(models.items())
        },
        "routing": config.routing.model_dump(mode="json"),
        "cache": config.cache.model_dump(mode="json"),
        "singleflight": config.singleflight.model_dump(mode="json"),
        "budget": config.budget.model_dump(mode="json"),
    }


def _logical_model_summary(model: Any) -> dict[str, Any]:
    return {
        "aliases": list(model.aliases),
        "fallbacks": list(model.fallbacks),
        "deployments": [_deployment_summary(deployment) for deployment in model.deployments],
    }


def _deployment_summary(deployment: Any) -> dict[str, Any]:
    return {
        "name": deployment.name,
        "provider": deployment.provider.value,
        "model": deployment.model,
        "enabled": deployment.enabled,
    }
