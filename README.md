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

## Environment

`.env.example` is grouped by topic:

- Model and runtime: model id, device, chunk size, upload limit.
- History: retention and storage path.
- Default languages: source and target language.
- OCR: enable flag and OCR language.
- Basic auth: login protection for the UI and API.
- Idle memory handling: unload the cached model after inactivity.

Planned environment-based features:

- HTTPS support.
- Reverse proxy support.

## Notes

- Default port: `5051`
- GPU test profile: `docker compose --profile gpu up -d lingumachina-gpu`
- OCR build: `docker compose build --build-arg INSTALL_OCR=true`
- Docker image: `registry.gitlab.com/marschu/lingumachina:latest`

## API

Main endpoints:

- `GET /health`
- `GET /languages`
- `POST /jobs/translate`
- `POST /jobs/translate-pdf`
- `GET /jobs/{job_id}`
- `POST /jobs/{job_id}/pause`
- `POST /jobs/{job_id}/resume`
- `POST /jobs/{job_id}/cancel`
- `GET /history`
- `POST /export-pdf`
- `POST /export-pdf-overlay`

## License

AGPL-3.0-or-later. See `LICENSE`.
