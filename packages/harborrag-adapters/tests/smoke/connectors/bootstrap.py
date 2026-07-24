"""Shared setup for standalone connector smoke scripts.

Connectors and parsers are built from the same declarative sources as the real
application: `config/connectors.yaml` and `config/parsers.yaml` (falling back
to the `.example.yaml` templates when a real file hasn't been created yet).
Environment variables come from `env/.env.connector` and `env/.env.parser`.
"""

from __future__ import annotations

import os
import re
import reprlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

REPO_ROOT = Path(__file__).resolve().parents[5]

for source_path in (
    REPO_ROOT / "packages" / "harborrag-adapters" / "src",
    REPO_ROOT / "packages" / "harborrag-core" / "src",
    REPO_ROOT / "packages" / "harborrag-runtime" / "src",
):
    source = str(source_path)
    if source not in sys.path:
        sys.path.insert(0, source)

from harborrag_adapters.parsers import BaseParser  # noqa: E402
from harborrag_core.domain.element import DocumentElement  # noqa: E402
from harborrag_core.domain.parser import ParsedDocument, ParseInput  # noqa: E402
from harborrag_core.security.redaction import redact_secrets  # noqa: E402
from harborrag_runtime.config import (  # noqa: E402
    ConnectorConfigurationError,
    load_connector_catalog,
    load_parser_catalog,
)
from harborrag_runtime.config.connectors.providers import config_factory  # noqa: E402

if TYPE_CHECKING:
    from harborrag_adapters.connectors import HarborConnector
    from harborrag_adapters.connectors.attachments.processing import CustomAttachmentParser
    from harborrag_adapters.parsers import HarborParser

CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_ENV_FILES = (
    REPO_ROOT / "env" / ".env.connector",
    REPO_ROOT / "env" / ".env.parser",
)
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"

_ATTACHMENT_PROVIDERS = frozenset({"confluence", "jira"})


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env() -> list[Path]:
    """Load smoke environment variables without overwriting exported ones.

    Defaults to `env/.env.connector` and `env/.env.parser`. Set
    `HARBOR_SMOKE_ENV_FILE` to load exactly one file instead.
    """
    configured_path = os.getenv("HARBOR_SMOKE_ENV_FILE")
    candidates = (
        [Path(configured_path).expanduser()] if configured_path else list(DEFAULT_ENV_FILES)
    )

    loaded: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name and name not in os.environ:
                os.environ[name] = _unquote(value)
        loaded.append(candidate)
    return loaded


def env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value else None


def _catalog_source(filename: str) -> Path:
    """Prefer a real `config/<filename>.yaml`; fall back to its example."""
    real = CONFIG_DIR / f"{filename}.yaml"
    if real.exists():
        return real
    example = CONFIG_DIR / f"{filename}.example.yaml"
    print(f"[config] {real.name} not found; falling back to {example.name}")
    return example


def connector_catalog():
    return load_connector_catalog(_catalog_source("connectors"))


def parser_catalog():
    return load_parser_catalog(_catalog_source("parsers"))


def _connector_environment(provider: str) -> dict[str, str]:
    """Copy `os.environ` with smoke-only aliases resolved for one provider."""
    values = dict(os.environ)
    if provider == "jira" and not values.get("JIRA_TOKEN") and values.get("JIRA_API_TOKEN"):
        values["JIRA_TOKEN"] = values["JIRA_API_TOKEN"]
    return values


def build_connector(
    name: str,
    *,
    include_attachments: bool,
    parser: HarborParser | None = None,
) -> HarborConnector:
    """Build one configured connector from `config/connectors.yaml`.

    Raises:
        ConnectorConfigurationError: If the connector is undefined or a
            referenced environment variable is missing/empty.
    """
    from harborrag_adapters.connectors import HarborConnector

    definition = connector_catalog().get(name)
    overrides: dict[str, Any] = {}
    if definition.provider in _ATTACHMENT_PROVIDERS:
        overrides["include_attachments"] = include_attachments
        if include_attachments:
            overrides["custom_parsers"] = attachment_custom_parsers()
        if definition.provider == "jira" and not definition.settings.get("project_keys"):
            project_key = env("JIRA_PROJECT_KEY")
            if project_key:
                overrides["project_keys"] = [project_key]

    values = definition.resolve_settings(
        environment=_connector_environment(definition.provider),
        overrides=overrides,
    )
    factory = config_factory(definition.provider)
    try:
        provider_config = factory(**values)
    except (TypeError, ValueError) as exc:
        raise ConnectorConfigurationError(
            f"Connector {name!r} ({definition.provider}) is invalid: {exc}"
        ) from exc

    extra = {"parser": parser} if definition.provider in _ATTACHMENT_PROVIDERS else {}
    return HarborConnector(definition.provider, config=provider_config, **extra)


class RapidOcrImageParser(BaseParser[ParseInput, ParsedDocument]):
    """Route image OCR through RapidOCR instead of the default pytesseract parser.

    Subclassing `BaseParser` (rather than duck-typing) matters here: its
    `__init_subclass__` normalizes `suffixes` to the dot-prefixed form
    `HarborParser`'s suffix routing actually indexes on (`"png"` -> `".png"`).
    A plain class with dot-less suffixes silently never matches by suffix —
    it would only ever route by `content_type`, which local files don't set.
    """

    parser_name: ClassVar[str] = "image"
    parser_engine: ClassVar[str] = "rapidocr"
    suffixes: ClassVar[frozenset[str]] = frozenset(
        {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "gif", "webp"}
    )
    content_types: ClassVar[frozenset[str]] = frozenset(
        {
            "image/png",
            "image/jpeg",
            "image/jpg",
            "image/tiff",
            "image/bmp",
            "image/gif",
            "image/webp",
        }
    )

    def parse(self, input: ParseInput) -> ParsedDocument:
        parse_input = self.coerce_input(input)
        from harborrag_adapters.parsers.input_loading import (
            parse_input_suffix,
            read_parse_input_bytes,
        )

        text = _parse_image_with_rapidocr(
            read_parse_input_bytes(parse_input),
            parse_input_suffix(parse_input),
        )
        elements = (
            [
                DocumentElement(
                    id="image:ocr:0",
                    type="image",
                    content=text,
                    metadata={"filename": parse_input.filename},
                )
            ]
            if text
            else []
        )
        return ParsedDocument(
            content=text,
            elements=elements,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            metadata=self.metadata_for(parse_input),
        )


def build_harbor_parser() -> HarborParser:
    """Assemble the parser stack from `config/parsers.yaml` (PDF via Docling)

    and swap in RapidOCR for plain images, since RapidOCR routing isn't part of
    the declarative parser catalog schema.
    """
    harbor_parser = parser_catalog().build_harbor_parser(environment=os.environ)
    harbor_parser.register(RapidOcrImageParser(), replace=True)
    return harbor_parser


def attachment_custom_parsers() -> dict[Any, CustomAttachmentParser]:
    """Route image attachments (Confluence/JIRA) to RapidOCR."""
    from harborrag_adapters.connectors.attachments.processing import FileType

    return {FileType.IMAGE: _parse_image_with_rapidocr}


_RAPID_OCR_ENGINE: Any | None = None


def _rapidocr_engine():
    """Build one RapidOCR engine and reuse its loaded ONNX models."""
    global _RAPID_OCR_ENGINE
    if _RAPID_OCR_ENGINE is None:
        try:
            import onnxruntime
            from rapidocr import RapidOCR
        except ImportError as exc:
            raise RuntimeError(
                "RapidOCR image parsing requires `harborrag-adapters[pdf]`, "
                "which installs both `rapidocr` and `onnxruntime`"
            ) from exc
        _RAPID_OCR_ENGINE = RapidOCR()
        providers = onnxruntime.get_available_providers()
        print(
            "[attachments] RapidOCR runtime='onnxruntime' "
            f"provider='CPUExecutionProvider' available_providers={providers!r}"
        )
    return _RAPID_OCR_ENGINE


def _parse_image_with_rapidocr(content: bytes, extension: str) -> str:
    """Extract ordered text lines from one image with RapidOCR."""
    _ = extension
    result = _rapidocr_engine()(content)
    texts = getattr(result, "txts", None) or ()
    return "\n".join(str(text).strip() for text in texts if str(text).strip())


PREVIEW_CHARS = 200
_VERBOSE_VALUES = {"1", "true", "yes", "on"}


def _verbose_previews_enabled() -> bool:
    """Require an explicit local opt-in before showing provider content."""
    if os.getenv("CI"):
        return False
    return os.getenv("HARBOR_SMOKE_VERBOSE", "").strip().lower() in _VERBOSE_VALUES


def _preview(value: object, *, limit: int = PREVIEW_CHARS) -> str:
    """Render a bounded, redacted preview without formatting whole large values."""
    if isinstance(value, str):
        total = len(value)
        text = value[:limit]
    elif isinstance(value, bytes):
        total = len(value)
        text = repr(value[:limit])
    else:
        renderer = reprlib.Repr()
        renderer.maxstring = limit
        renderer.maxother = limit
        renderer.maxdict = 10
        renderer.maxlist = 10
        text = renderer.repr(value)
        total = len(text)

    text = redact_secrets(text)
    if total <= limit:
        return text
    return f"{text}… (truncated, {total} chars total)"


def print_document(provider: str, document) -> None:
    verbose = _verbose_previews_enabled()
    print(f"\n[{provider}] loaded document")
    print(f"[{provider}] id={document.id!r}")
    print(f"[{provider}] content_type={document.content_type!r}")
    text = document.text()
    print(f"[{provider}] chars={len(text)}")
    if verbose:
        print(f"[{provider}] source preview={_preview(document.source)!r}")
        print(f"[{provider}] content preview={_preview(text)!r}")
        print(f"[{provider}] metadata preview={_preview(document.metadata)!r}")
        if document.raw is not None:
            print(f"[{provider}] raw preview={_preview(document.raw)!r}")
    print_attachments(provider, document, verbose=verbose)


def print_attachments(provider: str, document, *, verbose: bool = False) -> None:
    attachments = (document.metadata or {}).get("attachments") or []
    if not attachments:
        print(f"[{provider}] attachments: none")
        return
    print(f"[{provider}] attachments: {len(attachments)}")
    for index, attachment in enumerate(attachments, start=1):
        title = attachment.get("title")
        status = attachment.get("status")
        size_bytes = attachment.get("size_bytes")
        text = attachment.get("text") or ""
        reason = attachment.get("reason")
        line = f"  - attachment={index} status={status!r} "
        line += f"size_bytes={size_bytes} text_chars={len(text)}"
        if verbose and title:
            line += f" title={_preview(title)!r}"
        if verbose and reason:
            line += f" reason={_preview(reason)!r}"
        print(line)
        if verbose and text:
            print(f"    preview={_preview(text)!r}")


def print_parsed(provider: str, parsed: ParsedDocument, *, source: str) -> None:
    """Print a parsed local document the same shape as `print_document`."""
    verbose = _verbose_previews_enabled()
    print(f"\n[{provider}] parsed document")
    print(f"[{provider}] source={source!r}")
    print(f"[{provider}] parser={parsed.parser_name!r}")
    print(f"[{provider}] chars={len(parsed.content)}")
    print(
        f"[{provider}] elements={len(parsed.elements or [])} warnings={len(parsed.warnings or [])}"
    )
    if verbose:
        print(f"[{provider}] content preview={_preview(parsed.content)!r}")
        if parsed.metadata:
            print(f"[{provider}] metadata preview={_preview(parsed.metadata)!r}")


def format_metadata_value(value: Any) -> str | None:
    """Render one metadata value for a Markdown bullet, or `None` to skip it.

    Metadata dicts are full of fields that are frequently absent (due date,
    resolution, breadcrumb, ...); skipping empty ones keeps the rendered
    section a dense summary instead of a wall of blank/`None` bullets.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value if item not in (None, "")]
        return ", ".join(items) if items else None
    text = str(value).strip()
    return text or None


def render_metadata_section(metadata: dict[str, Any], fields: list[tuple[str, str]]) -> list[str]:
    """Render a `## Metadata` section from a curated `(label, key)` field list."""
    lines = [
        f"- **{label}**: {rendered}"
        for label, key in fields
        if (rendered := format_metadata_value(metadata.get(key))) is not None
    ]
    if not lines:
        return []
    return ["## Metadata", "", *lines, ""]


def attachments_passed(provider: str, document) -> bool:
    """Require every attempted attachment to avoid an unsupported/failed state."""
    attachments = (document.metadata or {}).get("attachments") or []
    failures = [
        attachment
        for attachment in attachments
        if attachment.get("status") in {"failed", "unsupported"}
    ]
    if not failures:
        return True
    print(f"[{provider}] attachment smoke failed: {len(failures)} attachment(s) failed")
    return False


def print_failure(provider: str, exc: Exception) -> None:
    """Print a bounded, redacted smoke failure instead of a traceback."""
    detail = redact_secrets(str(exc)).replace("\r", " ").replace("\n", " ")[:500]
    print(f"[{provider}] failed: {type(exc).__name__}: {detail}")


SUPPORTED_OUTPUT_FORMATS = ("txt", "md")


def sanitize_output_id(record_id: str) -> str:
    """Turn a record id/filename into a safe path segment shared by every saver."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", record_id).strip("_") or "document"


def output_path_for(
    provider: str,
    record_id: str,
    output: str,
    output_dir: Path | None = None,
) -> Path:
    """Compute where `save_output` will write, without writing anything.

    Callers that need to save attachment assets alongside the output file
    (so a saved `.md` can embed working image links) must know this path
    before the text to embed those links even exists.
    """
    target_dir = output_dir or DEFAULT_OUTPUT_DIR
    safe_id = sanitize_output_id(record_id)
    return target_dir / f"{provider}-{safe_id}.{output}"


def save_output(
    provider: str,
    record_id: str,
    text: str,
    *,
    output: str | None,
    output_dir: Path | None = None,
) -> Path | None:
    """Write parsed output to a file when `--output` was requested."""
    if not output:
        return None
    if output not in SUPPORTED_OUTPUT_FORMATS:
        choices = ", ".join(SUPPORTED_OUTPUT_FORMATS)
        raise ValueError(f"Unsupported output format {output!r}; choose: {choices}")

    target_path = output_path_for(provider, record_id, output, output_dir)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(text, encoding="utf-8")
    print(f"[{provider}] saved parsed output to {target_path}")
    return target_path


def save_attachment_asset(
    output_path: Path,
    title: str,
    content: bytes,
) -> Path:
    """Save one downloaded attachment next to its saved output file.

    Assets live in a `<output-file-stem>.assets/` sibling directory so an `.md`
    file can embed `![title](stem.assets/title)` and actually resolve.
    """
    assets_dir = output_path.parent / f"{output_path.stem}.assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    safe_name = sanitize_output_id(title) or "attachment"
    asset_path = assets_dir / safe_name
    asset_path.write_bytes(content)
    return asset_path
