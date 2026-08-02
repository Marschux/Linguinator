# NLLB Translate UI

Local translation UI and API based on Meta NLLB.

## Features

- Browser UI on port `5051`
- Text translation with chunking
- Text-PDF extraction to Markdown
- Text-PDF translation to Markdown
- Progress, ETA, pause, resume, and stop for background jobs
- Local conversion history with configurable retention

## Run

```bash
cp .env.example .env
docker network create proxy-net
docker compose up -d --build
```

Open:

```text
http://localhost:5051/
```

## System Requirements

Recommended CPU setup for local/LXC CPU inference:

- 4 vCPU minimum for usable local translation.
- 6-8 vCPU is a good practical range for better throughput.
- More cores can help, but gains usually flatten after 8 vCPU for single jobs.
- More cores help more when multiple jobs run at the same time.
- CPU clock speed and memory bandwidth can matter as much as core count.

Recommended memory:

- 8 GB RAM minimum for the default model and normal text/PDF jobs.
- 12 GB RAM or more is safer for larger PDFs and OCR-enabled images.

## GitLab Image

The CI pipeline builds and pushes:

```text
registry.gitlab.com/marschu/nllb-translate-ui:latest
registry.gitlab.com/marschu/nllb-translate-ui:<commit-short-sha>
registry.gitlab.com/marschu/nllb-translate-ui:<git-tag>
registry.gitlab.com/marschu/nllb-translate-ui:latest-ocr
registry.gitlab.com/marschu/nllb-translate-ui:<commit-short-sha>-ocr
registry.gitlab.com/marschu/nllb-translate-ui:<git-tag>-ocr
```

Homelab deployments should use that image and run with only `compose.yml` plus `.env`.

## Environment

```env
NLLB_MODEL=facebook/nllb-200-distilled-600M
NLLB_DEVICE=cpu
NLLB_MAX_CHARS=6000
NLLB_MAX_FILE_MB=50
NLLB_HISTORY_DAYS=7
NLLB_HISTORY_DIR=/data/history
NLLB_DEFAULT_SOURCE=eng_Latn
NLLB_DEFAULT_TARGET=deu_Latn
NLLB_ENABLE_OCR=false
NLLB_OCR_LANGUAGE=deu+eng
```

## OCR

OCR is optional and only runs when `NLLB_ENABLE_OCR=true`.

Current behavior:

- PDF pages with no extractable text use OCR when enabled.
- PDF pages with very little extractable text use OCR when enabled.
- OCR text reuses the existing Markdown and translation flow.
- OCR images are tagged with the `-ocr` suffix.

OCR adds Docker image size and CPU cost. For local builds, use:

```bash
docker compose build --build-arg INSTALL_OCR=true
```

## PDF Pages

PDF extraction and translation accept page ranges such as:

```text
1-3,5
```

## PDF Overlay Export

PDF translations can be downloaded in two PDF modes:

- Text PDF: creates a new clean PDF from the translated text.
- Overlay PDF: keeps the original PDF pages as the background and places translated text over them.

Overlay export can optionally cover the old text area with a light rectangle before placing the translation. This is a practical preview/export mode, not a full layout reconstruction.

Known overlay limits:

- Original text may remain visible outside the covered area.
- Tables, columns, forms, footnotes, and complex reading order are not reconstructed.
- Original fonts, exact line breaks, images, and text block positions are not matched.
- Long translated text may not fit the original page layout.

## Original Format Export

For loaded source files, `Originalformat` can export translated content back into a copy of the original container:

- DOCX: replaces paragraph text nodes while keeping the DOCX package structure.
- ODT: replaces text paragraphs in `content.xml`.
- XLSX: replaces selected sheet cells in selected columns.
- CSV: replaces selected columns and writes CSV again.

This is a conservative 1:1 mode. It does not rebuild complex layout, styling, formulas, comments, tracked changes, headers, footers, or embedded objects. A later PDF layout mode may extract images, detect text blocks, and rebuild a new PDF layout, but that is separate from the current overlay and text-PDF modes.

## GPU Profile

The default compose service stays CPU-first. For a local GPU test service on port `5052`:

```bash
docker compose --profile gpu up -d nllb-translate-gpu
```

## License

GNU Affero General Public License v3.0 or later. See `LICENSE`.

## API

- `GET /health`
- `GET /languages`
- `POST /translate`
- `POST /extract-pdf`
- `POST /extract-docx`
- `POST /extract-odt`
- `POST /extract-csv`
- `POST /extract-xlsx`
- `POST /translate-pdf`
- `POST /jobs/translate`
- `POST /jobs/translate-pdf`
- `GET /jobs/{job_id}`
- `POST /jobs/{job_id}/pause`
- `POST /jobs/{job_id}/resume`
- `POST /jobs/{job_id}/cancel`
- `GET /history`
- `GET /history/{history_id}`
- `POST /export-pdf`
- `POST /export-pdf-overlay`
- `POST /export-docx`
- `POST /export-odt`
- `POST /export-csv`
- `POST /export-xlsx`
