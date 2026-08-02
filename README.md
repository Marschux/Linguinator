# Lingumachina

Lokale Dokument-Uebersetzung mit Meta NLLB und Browser-UI.

## Start

```bash
cp .env.example .env
docker network create proxy-net
docker compose up -d --build
```

Danach im Browser oeffnen:

```text
http://localhost:5051/
```

## Funktionen

- Text- und Dokumentuebersetzung mit Fortschritt, Pause, Fortsetzen und Stop.
- PDF-Extraktion, PDF-Uebersetzung und PDF-Download.
- Originalformat-Export fuer DOCX, ODT, PPTX, CSV, XLSX, HTML, SRT/VTT, JSON/YAML, PO und XLIFF.
- Lokale History mit einstellbarer Aufbewahrung.
- Optionaler Passwortschutz per HTTP Basic Auth.
- Optionales OCR fuer gescannte PDFs.

## Konfiguration

Wichtige Variablen in `.env`:

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
LINGUMACHINA_AUTH_ENABLED=false
LINGUMACHINA_AUTH_USERNAME=admin
LINGUMACHINA_AUTH_PASSWORD=
LINGUMACHINA_UNLOAD_MODEL_AFTER_IDLE=true
LINGUMACHINA_MODEL_IDLE_SECONDS=1200
```

Wenn `LINGUMACHINA_AUTH_ENABLED=true` gesetzt ist, muss `LINGUMACHINA_AUTH_PASSWORD` gefuellt sein. `/health` bleibt ohne Login erreichbar.

## Hinweise

- Standardport: `5051`
- GPU-Testprofil: `docker compose --profile gpu up -d lingumachina-gpu`
- OCR-Build: `docker compose build --build-arg INSTALL_OCR=true`
- Docker-Image: `registry.gitlab.com/marschu/lingumachina:latest`

## API

Die wichtigsten Endpunkte:

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

## Lizenz

AGPL-3.0-or-later. Siehe `LICENSE`.
