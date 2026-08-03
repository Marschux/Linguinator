# Lingumachina

Local document translation workbench powered by Meta NLLB.

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
- Original-format export for DOCX, ODT, PPTX, CSV, XLSX, HTML, SRT/VTT, JSON/YAML, PO, and XLIFF.
- Local history with configurable retention, retained source files, and Markdown, TXT, PDF, or original-format downloads.
- Persistent global job queue with configurable worker count.
- Interface language selector for English, German, Spanish, and French.
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
| `LINGUMACHINA_MODEL` | `facebook/nllb-200-distilled-600M` | Hugging Face model id used for translation. |
| `LINGUMACHINA_DEVICE` | `cpu` | Runtime device. Use `cuda` with the GPU compose profile and a CUDA-capable host. |
| `LINGUMACHINA_MAX_CHARS` | `6000` | Maximum characters per translation chunk. Longer input is split into multiple chunks. |
| `LINGUMACHINA_MAX_FILE_MB` | `50` | Maximum upload size in megabytes. |
| `LINGUMACHINA_CPU_THREADS` | `0` | Optional Torch, OMP, and MKL thread count for CPU translation. `0` keeps library defaults. |
| `LINGUMACHINA_CPU_INTEROP_THREADS` | `0` | Optional Torch inter-op thread count. `0` keeps library defaults. |
| `LINGUMACHINA_HISTORY_DAYS` | `7` | Number of days to keep saved translation history, including retained source files. |
| `LINGUMACHINA_HISTORY_DIR` | `/data/history` | Directory for saved history files, source files, and metadata inside the container. |
| `LINGUMACHINA_JOB_WORKERS` | `1` | Number of queued translation jobs that may run in parallel. Higher values can use more CPU/RAM. |
| `LINGUMACHINA_JOBS_DIR` | `/data/history/jobs` | Directory for persisted queue metadata and pending PDF payloads. |
| `LINGUMACHINA_DEFAULT_SOURCE` | `eng_Latn` | Default source language code. |
| `LINGUMACHINA_DEFAULT_TARGET` | `deu_Latn` | Default target language code. |
| `LINGUMACHINA_ENABLE_OCR` | `false` | Enables OCR fallback for scanned PDFs when the image dependencies are available. |
| `LINGUMACHINA_OCR_LANGUAGE` | `deu+eng` | OCR language setting passed to Tesseract. |
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

Older `NLLB_*` variables are still accepted as fallback, but new setups should use the `LINGUMACHINA_*` names.
Queue worker count defaults to one because multiple simultaneous model jobs can increase memory usage sharply, especially with larger models.

## Notes

- Default port: `5051`
- HTTPS is best terminated by Caddy, Traefik, Nginx, or another reverse proxy.
- GPU test profile: `docker compose -f docker/compose.yml --profile gpu up -d lingumachina-gpu`
- OCR is installed by default in the container image; keep the service image current and restart after updates.
- Docker image: `registry.gitlab.com/marschu/lingumachina:latest`

## License

AGPL-3.0-or-later. See `LICENSE`.

The default model `facebook/nllb-200-distilled-600M` is provided by Meta under CC-BY-NC-4.0. It is intended for non-commercial research use. Commercial use, paid services, or production deployments need a different model or separate permission from the model rights holder.

Model license and card:

- https://huggingface.co/facebook/nllb-200-distilled-600M
- https://creativecommons.org/licenses/by-nc/4.0/

For commercial use, keep `LINGUMACHINA_MODEL` configurable and replace the default model with one that explicitly allows the intended use. Practical options are:

- use an existing translation model with a suitable commercial license,
- fine-tune a commercially usable base model on properly licensed parallel texts,
- train a dedicated translation model from licensed data if the required quality, domain, or language pair justifies the cost.

Always check both the model license and the training data rights before offering hosted, paid, or customer-facing translation.
