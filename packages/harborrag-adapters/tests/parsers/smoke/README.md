# Parser smoke checks

`parse_file.py` parses one real local document through Harbor's public parser interfaces, without
pytest or test doubles. A pass shows that the file can be read, the chosen parser runs, and the
extracted text is not empty.

Read the shared [safety rules and exit codes](../../README.md#real-system-smoke-tests) before you
use sensitive documents or engines that download models.

## Before you start

- Run every command from the repository root.
- Use the workspace environment from [CONTRIBUTING](../../../../../CONTRIBUTING.md#development-setup).
  The root `dev` dependency group adds `harborrag-adapters[parsers,pdf-liteparse]`, which covers
  the built-in formats and the PyMuPDF and LiteParse PDF backends. Do not narrow the sync to one
  package, because `uv sync` then removes the rest of the workspace from the environment.
- For the Docling, MinerU and PaddleOCR backends, add the adapters `pdf` extra:

  ```bash
  uv sync --all-packages --extra dev --extra production --extra pdf
  ```

  Some engines also need model downloads, native libraries, or enough CPU, GPU and memory, so an
  installed backend can still fail to run on a given machine.
- `parse_file.py` loads no dotenv file. If a backend needs credentials, model-cache settings or OCR
  settings, export the variables first; `env-example/.env.parser.example` lists them.
- Use representative, non-sensitive files. Keep a small corpus with at least one DOCX, PPTX, XLSX,
  CSV, HTML, EPUB, image, Markdown or text file, JSON file, text PDF and scanned PDF.

## Parse a PDF with one backend

`--pdf-backend` runs exactly one PDF backend: `docling`, `liteparse`, `mineru`, `paddleocr` or
`pymupdf`.

```bash
uv run python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py \
  path/to/report.pdf --pdf-backend pymupdf
```

A pass prints one line, for example:

```text
[parsers] passed parser='pdf' chars=68 elements=1 warnings=0
```

## Automatic routing and PDF profiles

Without a PDF option, the script lets the parser registry pick a parser from the file type.
`--pdf-profile` instead runs a built-in PDF profile: `liteparse`, `fast`, `balanced`, `ocr`,
`ocr_first`, `quality` or `scientific`. `--pdf-profile` and `--pdf-backend` cannot be combined, and
either one with a file that is not a PDF exits `1`.

```bash
uv run python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py path/to/notes.md
uv run python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py \
  path/to/report.pdf --pdf-profile fast
```

Automatic routing uses the same registry as ingestion, and a profile builds its PDF parser the way
ingestion does, through `HarborParserFactory`. A profile whose engines are not installed fails with
the engine's import error.

## What the output shows

The script passes only when parsing finishes and the extracted text is not empty. It prints the
parser name and the character, element and warning counts. It never prints or saves the extracted
text.

| Code | Meaning |
| --- | --- |
| `0` | Parsing succeeded and returned text |
| `1` | The selection was invalid, the parser failed, or it returned no text |
| `2` | The input file does not exist |

## Troubleshooting

- No parser matches the file: check that its suffix or content type is a supported format.
- A backend fails to import or load its model: install the `pdf` extra, then check that backend's
  native libraries, model cache, memory, CPU or GPU, and network access.
- A scanned PDF returns no text: retry with an OCR backend such as `liteparse` or `paddleocr`.
- The result differs from a deployment: run with the same Python environment, and record the
  parser, backend and installed dependency versions.
