# Parser smoke checks

`parse_file.py` parses one real local document through Harbor's public parser
interfaces. It verifies file access, automatic registry routing or an explicit
PDF selection, and non-empty extracted content without pytest or test doubles.

Read the shared [smoke-test safety and exit-code
guidance](../../README.md#real-system-smoke-tests) before using sensitive
documents or engines that download models.

## Prerequisites

- Run commands from the repository root with Python 3.12.
- Use representative, non-sensitive files from the target environment.
- Install the standard parser dependencies:

```bash
uv sync --package harborrag-adapters --extra parsers
```

For optional PDF profiles and exact backends, install the PDF engines as well:

```bash
uv sync --package harborrag-adapters --extra parsers --extra pdf
```

Some PDF engines require model downloads, native libraries, or suitable
CPU/GPU resources. Installation alone does not guarantee that every backend is
runnable on the current machine.

`parse_file.py` does not load dotenv files. If a backend needs credentials,
model-cache controls, or OCR settings, use
`env-example/.env.parser.example` as a reference
and export the selected variables in the process environment before running the
command.

## Run automatic routing

Pass a real document path and let `HarborParser` select the parser:

```bash
python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py samples/report.docx
python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py samples/data.xlsx
python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py samples/report.pdf
```

Maintain a representative corpus with at least one DOCX, PPTX, XLSX, CSV,
HTML, EPUB, image, Markdown/text, JSON, text PDF, and scanned PDF.

## Select PDF processing

For PDFs, choose either a Harbor profile or one exact backend. The options are
mutually exclusive.

```bash
python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py \
  samples/report.pdf --pdf-profile fast

python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py \
  samples/scan.pdf --pdf-profile ocr

python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py \
  samples/report.pdf --pdf-backend pymupdf

python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py \
  samples/report.pdf --pdf-backend docling
```

Supported profiles come from `PDFParserProfile`. Exact backend choices are
`docling`, `liteparse`, `mineru`, `paddleocr`, and `pymupdf`. PDF options used
with a non-PDF input return exit code `1`.

## Success criteria and output

The check passes only when parsing completes and extracted content is non-empty.
It prints only:

- selected parser name;
- extracted character count;
- element count; and
- warning count.

It does not print or persist extracted content.

## Exit codes and troubleshooting

| Code | Meaning |
| --- | --- |
| `0` | Parsing succeeded with non-empty content |
| `1` | Selection was invalid, the parser failed, or content was empty |
| `2` | The requested real input file does not exist |

- Routing failure: confirm the file suffix/content type is supported and the
  `parsers` extra is installed.
- Backend import/model failure: install the `pdf` extra and check native,
  model-cache, memory, CPU/GPU, and network requirements for that backend.
- Empty scanned PDF: retry with an OCR-capable profile or exact OCR backend.
- Different result in deployment: run from the same Python environment and
  record the selected parser/backend and installed dependency versions.
