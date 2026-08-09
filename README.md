# Linguinator

Local document translation workbench powered by OPUS-MT.

## Start

```bash
cp docker/.env.example .env
docker network create proxy-net
docker compose -f docker/compose.yml up -d --build
```

Open:

```text
http://localhost:5051/
```

## Features

- Text and document translation with progress, pause, resume, and stop.
- PDF extraction, PDF translation, and PDF download.
- Layout-preserving PDF translation: the translation is placed back into the original lines, images and graphics stay untouched. Falls back automatically to a plain OCR-capable extraction for scanned/image-only PDFs. A plain text/Markdown/PDF/DOCX version is always available afterwards via the download buttons.
- Original-format export for DOCX, ODT, PPTX, CSV, XLSX, HTML, SRT/VTT, JSON/YAML, PO, and XLIFF.
- Local history with configurable retention, retained source files, and Markdown, TXT, PDF, DOCX, or original-format downloads.
- Persistent global job queue with configurable worker count.
- Interface language selector for English, German, Spanish, and French.
- Optional HTTP Basic Auth.
- OCR fallback for scanned PDFs, always on when the image includes the OCR binaries.

## Usage

1. Select the source and target language.
2. Choose an input tab:
   - `Text Field`: translate pasted or typed text directly.
   - `Text`: load plain text and supported structured text files.
   - `Markdown`: load Markdown files.
   - `Doc File`: load DOCX or ODT files.
   - `PowerPoint`: load PPTX files.
   - `CSV File`: load CSV or XLSX files and optionally limit translation to selected columns.
   - `PDF`: extract and translate PDF pages.
3. For file inputs, select the file and click `Load File` if the tab uses the shared text input.
4. Click `Translate Input`.
5. Use `Pause`, `Resume`, or `Stop` for running jobs.
6. Review the preview and the history below it.
7. Choose a download format and click `Download`.

For large jobs, the browser tab title shows the current progress and job status.
History entries and retained source files are stored locally in the configured history directory and are cleaned up after the configured retention time.
For supported file inputs, `Original Format` is selected as the default download format after loading the file.

## Environment

Copy `docker/.env.example` to `.env` in the repo root before starting the container. Docker Compose reads this file automatically.

```bash
cp docker/.env.example .env
```

The `.env` file is grouped by topic:

| Variable | Default | Description |
| --- | --- | --- |
| `LINGUINATOR_MODEL` | `Helsinki-NLP/opus-mt-tc-bible-big-mul-mul` | Fallback model, used for any language pair without a dedicated model in `app/opus_pairs.json`. |
| `LINGUINATOR_MODEL_CACHE_SIZE` | `1` | How many models (dedicated pair models plus the fallback) stay loaded in memory at once. Each entry costs its own RAM; raise only if RAM allows and pairs alternate often. |
| `LINGUINATOR_LANGUAGES` | built-in core list | Overrides the language picker entirely. Comma-separated `xx:xxx_Scr` entries, e.g. `en:eng_Latn,de:deu_Latn`. |
| `LINGUINATOR_DEVICE` | `cpu` | Runtime device. Use `cuda` with the GPU compose profile and a CUDA-capable host. |
| `LINGUINATOR_MAX_CHARS` | `2000` | Maximum characters per translation chunk. Longer input is split into multiple chunks. Sized for OPUS-MT's ~512-token limit; a hard `TRANSLATE_MAX_TOKENS` cap in the code truncates the rare oversized chunk instead of crashing. |
| `LINGUINATOR_MAX_FILE_MB` | `50` | Maximum upload size in megabytes. |
| `LINGUINATOR_CPU_THREADS` | `0` | Optional Torch, OMP, and MKL thread count for CPU translation. `0` keeps library defaults. |
| `LINGUINATOR_CPU_INTEROP_THREADS` | `0` | Optional Torch inter-op thread count. `0` keeps library defaults. |
| `LINGUINATOR_HISTORY_DAYS` | `7` | Number of days to keep saved translation history, including retained source files. |
| `LINGUINATOR_HISTORY_DIR` | `/data/history` | Directory for saved history files, source files, and metadata inside the container. |
| `LINGUINATOR_TIMEZONE` | `UTC` | IANA timezone name (e.g. `Europe/Berlin`) used to display the completion time next to each history entry. |
| `LINGUINATOR_JOB_WORKERS` | `1` | Number of queued translation jobs that may run in parallel. Higher values can use more CPU/RAM. |
| `LINGUINATOR_JOBS_DIR` | `/data/history/jobs` | Directory for persisted queue metadata and pending PDF payloads. |
| `LINGUINATOR_DEFAULT_SOURCE` | `eng_Latn` | Default source language code. |
| `LINGUINATOR_DEFAULT_TARGET` | `deu_Latn` | Default target language code. |
| `LINGUINATOR_OCR_LANGUAGE` | `deu+eng` | OCR language setting passed to Tesseract. OCR itself is always on when `pdftoppm`/`tesseract` are installed, there is no toggle. |
| `LINGUINATOR_PDF_FONT` | empty | TrueType font embedded into generated PDFs. Defaults to DejaVu Sans from the image; needed for non-Latin target languages. Ignored for CJK/Arabic/Devanagari/Hebrew text, which always uses the bundled Noto fonts (DejaVu Sans has no glyphs for those scripts). |
| `LINGUINATOR_PDF_FONT_BOLD` | empty | Bold variant of the embedded PDF font. |
| `LINGUINATOR_PDF_LAYOUT_BATCH_SIZE` | `4` | How many layout-PDF paragraphs are translated in one model call. Higher trades more peak memory (padding to the longest paragraph in the batch) for fewer, faster calls. |
| `LINGUINATOR_AUTH_ENABLED` | `false` | Enables HTTP Basic Auth for the UI and API. `/health` stays public for health checks. |
| `LINGUINATOR_AUTH_USERNAME` | `admin` | Basic Auth username. |
| `LINGUINATOR_AUTH_PASSWORD` | `changeme` | Basic Auth password. Change this before enabling auth. |
| `LINGUINATOR_ROOT_PATH` | empty | URL prefix when the app is mounted below a reverse-proxy path, for example `/linguinator`. |
| `LINGUINATOR_PUBLIC_URL` | empty | Optional externally visible base URL reported by `/health`. |
| `LINGUINATOR_TRUST_PROXY_HEADERS` | `true` | Lets Uvicorn trust forwarded proxy headers. |
| `LINGUINATOR_FORWARDED_ALLOW_IPS` | `*` | IP allow-list for forwarded headers. Narrow this in stricter deployments. |
| `LINGUINATOR_SSL_CERTFILE` | empty | Optional certificate path for direct HTTPS inside the container. Usually leave empty behind a reverse proxy. |
| `LINGUINATOR_SSL_KEYFILE` | empty | Optional private key path for direct HTTPS inside the container. |
| `LINGUINATOR_UNLOAD_MODEL_AFTER_IDLE` | `true` | Unloads cached model objects after an idle period. |
| `LINGUINATOR_MODEL_IDLE_SECONDS` | `1200` | Idle time in seconds before unloading the model cache. |

Queue worker count defaults to one because multiple simultaneous model jobs can increase memory usage sharply, especially with larger models.

## Notes

- Default port: `5051`
- HTTPS is best terminated by Caddy, Traefik, Nginx, or another reverse proxy.
- GPU test profile: `docker compose -f docker/compose.yml --profile gpu up -d linguinator-gpu`
- OCR is installed by default in the container image; keep the service image current and restart after updates.
- Docker image: `registry.gitlab.com/marschu/linguinator:latest`

## License

AGPL-3.0-or-later. See `LICENSE`.

Translation is powered by the [OPUS-MT](https://github.com/Helsinki-NLP/Opus-MT) models from the
University of Helsinki's Language Technology Research Group (Helsinki-NLP), commercially usable
and licensed per model as either Apache-2.0 or CC-BY-4.0 (the latter requires attribution, given
here). `app/opus_pairs.json` (generated by `tools/generate_opus_pairs.py`) records the license of
every dedicated pair model actually in use; the fallback model,
`Helsinki-NLP/opus-mt-tc-bible-big-mul-mul`, is Apache-2.0.

Model cards: https://huggingface.co/Helsinki-NLP

Before deploying with a different `LINGUINATOR_MODEL` or an extended `app/opus_pairs.json`, check
that model's own license and training-data rights.
