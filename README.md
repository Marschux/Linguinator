# Lingumachina

Local document translation workbench powered by Meta NLLB.

## Start

```bash
cp .env.example .env
docker network create proxy-net
docker compose up -d --build
```

Open:

```text
http://localhost:5051/
```

## Features

- Text and document translation with progress, pause, resume, and stop.
- PDF extraction, PDF translation, and PDF download.
- Original-format export for DOCX, ODT, PPTX, CSV, XLSX, HTML, SRT/VTT, JSON/YAML, PO, and XLIFF.
- Local history with configurable retention.
- Optional HTTP Basic Auth.
- Optional OCR for scanned PDFs.

## Usage

1. Select the source and target language.
2. Choose an input tab:
   - `Text Field`: translate pasted or typed text directly.
   - `Text`: load plain text and supported structured text files.
   - `Markdown`: load Markdown files.
   - `Office Doc`: load DOCX, ODT, or PPTX files.
   - `CSV File`: load CSV or XLSX files and optionally limit translation to selected columns.
   - `PDF`: extract and translate PDF pages.
3. For file inputs, select the file and click `Load File` if the tab uses the shared text input.
4. Click `Translate Input`.
5. Use `Pause`, `Resume`, or `Stop` for running jobs.
6. Review the preview or open `History`.
7. Choose a download format and click `Download`.

For large jobs, the browser tab title shows the current progress and job status.
History entries are stored locally in the configured history directory and are cleaned up after the configured retention time.

## Environment

Copy `.env.example` to `.env` before starting the container. Docker Compose reads this file automatically.

```bash
cp .env.example .env
```

The `.env` file is grouped by topic:

| Variable | Default | Description |
| --- | --- | --- |
| `NLLB_MODEL` | `facebook/nllb-200-distilled-600M` | Hugging Face model id used for translation. |
| `NLLB_DEVICE` | `cpu` | Runtime device. Use `cuda` with the GPU compose profile and a CUDA-capable host. |
| `NLLB_MAX_CHARS` | `6000` | Maximum characters per translation chunk. Longer input is split into multiple chunks. |
| `NLLB_MAX_FILE_MB` | `50` | Maximum upload size in megabytes. |
| `NLLB_HISTORY_DAYS` | `7` | Number of days to keep saved translation history. |
| `NLLB_HISTORY_DIR` | `/data/history` | Directory for saved history files and metadata inside the container. |
| `NLLB_DEFAULT_SOURCE` | `eng_Latn` | Default source language code. |
| `NLLB_DEFAULT_TARGET` | `deu_Latn` | Default target language code. |
| `NLLB_ENABLE_OCR` | `false` | Enables OCR fallback for scanned PDFs when the image dependencies are installed. |
| `NLLB_OCR_LANGUAGE` | `deu+eng` | OCR language setting passed to Tesseract. |
| `LINGUMACHINA_AUTH_ENABLED` | `false` | Enables HTTP Basic Auth for the UI and API. `/health` stays public for health checks. |
| `LINGUMACHINA_AUTH_USERNAME` | `admin` | Basic Auth username. |
| `LINGUMACHINA_AUTH_PASSWORD` | `changeme` | Basic Auth password. Change this before enabling auth. |
| `LINGUMACHINA_ROOT_PATH` | empty | URL prefix when the app is mounted below a reverse-proxy path, for example `/lingumachina`. |
| `LINGUMACHINA_PUBLIC_URL` | empty | Optional externally visible base URL reported by `/health`. |
| `LINGUMACHINA_TRUST_PROXY_HEADERS` | `true` | Lets Uvicorn trust forwarded proxy headers. |
| `LINGUMACHINA_FORWARDED_ALLOW_IPS` | `*` | IP allow-list for forwarded headers. Narrow this in stricter deployments. |
| `LINGUMACHINA_SSL_CERTFILE` | empty | Optional certificate path for direct HTTPS inside the container. Usually leave empty behind a reverse proxy. |
| `LINGUMACHINA_SSL_KEYFILE` | empty | Optional private key path for direct HTTPS inside the container. |
| `LINGUMACHINA_UNLOAD_MODEL_AFTER_IDLE` | `true` | Unloads cached model objects after an idle period. |
| `LINGUMACHINA_MODEL_IDLE_SECONDS` | `1200` | Idle time in seconds before unloading the model cache. |

Basic Auth still accepts the older `NLLB_AUTH_*` variables as fallback, but new setups should use the `LINGUMACHINA_AUTH_*` names.

## Notes

- Default port: `5051`
- HTTPS is best terminated by Caddy, Traefik, Nginx, or another reverse proxy.
- GPU test profile: `docker compose --profile gpu up -d lingumachina-gpu`
- OCR build: `docker compose build --build-arg INSTALL_OCR=true`
- Docker image: `registry.gitlab.com/marschu/lingumachina:latest`

## License

AGPL-3.0-or-later. See `LICENSE`.
