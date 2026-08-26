from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import yaml

from .chat.configs import HarborChatClientConfig
from .embed.configs import HarborEmbedClientConfig
from .rerank.configs import HarborRerankClientConfig

type SupportedConfig = HarborChatClientConfig | HarborEmbedClientConfig | HarborRerankClientConfig


class ModelConfigCli:
    """Validate, render, and explain Harbor model configuration files."""

    def run(self, argv: Sequence[str] | None = None) -> int:
        """Parse command arguments and return a process-compatible exit status."""

        parser = _parser()
        args = parser.parse_args(argv)
        try:
            config = _load_config(args.family, args.file, args.profile)
            if args.command == "validate":
                print(f"valid {args.family} configuration: {args.file}")
                return 0
            if args.command == "render":
                _write_rendered(config, output=args.output, format_name=args.format)
                return 0
            if args.command == "explain":
                print(json.dumps(_explain(config), indent=2, sort_keys=True))
                return 0
        except Exception as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        parser.error(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Harbor model configuration command-line interface."""

    return ModelConfigCli().run(argv)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harborrag-models")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "render", "explain"):
        command = subcommands.add_parser(name)
        command.add_argument("file", type=Path)
        command.add_argument("--family", choices=("chat", "embed", "rerank"), required=True)
        command.add_argument("--profile")
        if name == "render":
            command.add_argument("--format", choices=("json", "yaml"), default="yaml")
            command.add_argument("--output", type=Path)
    return parser


def _load_config(family: str, path: Path, profile: str | None) -> SupportedConfig:
    types: dict[str, type[SupportedConfig]] = {
        "chat": HarborChatClientConfig,
        "embed": HarborEmbedClientConfig,
        "rerank": HarborRerankClientConfig,
    }
    return types[family].from_file(path, profile=profile)


def _write_rendered(config: SupportedConfig, *, output: Path | None, format_name: str) -> None:
    rendered = config.sanitized_dump()
    text = (
        json.dumps(rendered, indent=2, sort_keys=True)
        if format_name == "json"
        else yaml.safe_dump(rendered, sort_keys=True)
    )
    if output is None:
        print(text, end="" if text.endswith("\n") else "\n")
        return
    output.write_text(text, encoding="utf-8")


def _explain(config: SupportedConfig) -> dict[str, Any]:
    models = cast(dict[str, Any], config.models)
    return {
        "family": config.config_section,
        "default_model": config.default_model,
        "logical_models": {
            logical: {
                "aliases": list(model.aliases),
                "fallbacks": list(model.fallbacks),
                "deployments": [
                    {
                        "name": deployment.name,
                        "provider": getattr(deployment.provider, "value", str(deployment.provider)),
                        "model": deployment.model,
                        "enabled": deployment.enabled,
                    }
                    for deployment in model.deployments
                ],
            }
            for logical, model in sorted(models.items())
        },
        "routing": config.routing.model_dump(mode="json"),
        "cache": config.cache.model_dump(mode="json"),
        "singleflight": config.singleflight.model_dump(mode="json"),
        "budget": config.budget.model_dump(mode="json"),
    }
