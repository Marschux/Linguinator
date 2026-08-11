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
- Website translation (built, tab still disabled pending testing): paste the address of a publicly reachable page and the readable article is extracted and translated, downloadable as PDF, DOCX, Markdown, or TXT. **This is the one feature that reaches out to the internet.** Pages behind a login or paywall, and pages that build their content with JavaScript, cannot be read. Addresses inside your own network (localhost, private ranges, `.local`, cloud metadata) are refused, including after redirects.
- Original-format export for DOCX, ODT, PPTX, CSV, XLSX, HTML, SRT/VTT, JSON/YAML, PO, and XLIFF.
- Local history with configurable retention, retained source files, and Markdown, TXT, PDF, DOCX, or original-format downloads.
- Persistent global job queue with configurable worker count.
- Interface language selector for English, German, Spanish, and French.
- Optional HTTP Basic Auth.
- OCR fallback for scanned PDFs, always on when the image includes the OCR binaries. It reads in the source language picked for the job and covers every language the app offers. With the source set to auto-detect, the script is read off the page image first and the page is then read twice, so a scan finds its own language; that first page takes correspondingly longer. Pages printed in two scripts are split into their text blocks and each block is read in its own language, so a page mixing, say, Devanagari and Latin no longer loses one of them. `/health` lists what the running image can read.

## Usage

1. Select the source and target language.
2. Choose an input tab:
   - `Text Field`: translate pasted or typed text directly.
   - `Text`: load plain text and supported structured text files.
   - `Markdown`: load Markdown files.
   - `DOC File`: load DOCX or ODT files.
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
| `LINGUINATOR_MAX_FILE_MB` | `50` | Maximum upload size in megabytes. Office archives are additionally capped at ten times that once unpacked. |
| `LINGUINATOR_CPU_THREADS` | `0` | Torch, OMP and MKL thread count. `0` keeps the library defaults, which take every core; set a number when the container shares its host. |
| `LINGUINATOR_MODEL_IDLE_SECONDS` | `600` | Seconds of idleness before the loaded model is dropped from memory. It reloads on the next job, which costs a few seconds. `0` keeps it loaded for good. |
| `LINGUINATOR_HISTORY_DAYS` | `7` | Number of days to keep saved translation history, including retained source files. |
| `LINGUINATOR_TIMEZONE` | `Europe/Berlin` | IANA timezone name used to display the completion time next to each history entry. Display only, stored times are UTC. |
| `LINGUINATOR_TIME_FORMAT` | `24h` | `12h` or `24h`, the same for every UI language. Anything else is read as `24h`. |
| `LINGUINATOR_UI_LANGUAGE` | `en` | UI language a fresh browser starts with: `en` (English), `de` (German), `es` (Spanish), `fr` (French). A browser switched by hand keeps its own choice. |
| `LINGUINATOR_DEFAULT_TARGET` | `eng_Latn` | Preselected target language. The source starts on auto-detect and is not configurable. |
| `LINGUINATOR_FAVORITE_LANGUAGES` | `deu_Latn,spa_Latn,fra_Latn` | Favourites at the top of both language pickers, comma-separated. At most three are used; English is always added, so the list shows up to four. Codes not on offer are ignored. |
| `LINGUINATOR_AUTH_ENABLED` | `false` | Enables HTTP Basic Auth for the UI and API. `/health` stays public for health checks. |
| `LINGUINATOR_AUTH_USERNAME` | `Translator` | Basic Auth username. |
| `LINGUINATOR_AUTH_PASSWORD` | empty | Basic Auth password. Set one before enabling auth; while it is empty the app answers every request with 500 rather than letting anyone in. |
| `LINGUINATOR_ROOT_PATH` | empty | URL prefix when the app is mounted below a reverse-proxy path, for example `/linguinator`. |

Everything else is fixed in `app/main.py` rather than configurable, because it either has one right
answer here (one model in memory, one job at a time) or cannot be changed usefully from a `.env`
alone (font files and certificates would first have to be mounted into the container). The device
is not a setting either: the GPU is used when there is one, the CPU otherwise.

### Where data is stored

| Path in the container | Volume | Contents |
| --- | --- | --- |
| `/data/history` | `history` | Saved translations, retained source files, metadata. Cleaned up after `LINGUINATOR_HISTORY_DAYS`. |
| `/data/history/jobs` | `history` | Queue state and pending payloads, so jobs survive a restart. |
| `/cache/huggingface` | `hf-cache` | Downloaded models. Several GB once a few language pairs have been used; nothing removes them automatically. |

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

The fallback model is fixed in `app/main.py` rather than configurable, so that this section stays
true for every deployment. Before swapping it there, or extending `app/opus_pairs.json`, check that
model's own license and training-data rights.
