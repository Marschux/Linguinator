"""The FastAPI app, authentication and every route."""
import base64
import secrets
import json
import shutil
from typing import List, Union
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import config, langs, models, translation, textdocs, history, jobqueue, ocr, pdf_text, formats, pipeline


app = FastAPI(title="Linguinator", version="1.5.0", root_path=config.ROOT_PATH)
app.mount("/static", StaticFiles(directory=config.APP_DIR / "static"), name="static")


class TranslateRequest(BaseModel):
    q: Union[str, List[str]]
    source: str = config.DEFAULT_SOURCE
    target: str = config.DEFAULT_TARGET


class PdfExportRequest(BaseModel):
    text: str


class DocxExportRequest(BaseModel):
    text: str


def unauthorized_response() -> Response:
    return Response(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Linguinator"'},
    )


def basic_auth_valid(header: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except Exception:
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    return secrets.compare_digest(username, config.AUTH_USERNAME) and secrets.compare_digest(password, config.AUTH_PASSWORD)


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if not config.AUTH_ENABLED or request.url.path == "/health":
        return await call_next(request)
    if not config.AUTH_PASSWORD:
        return Response("Authentication is enabled but LINGUINATOR_AUTH_PASSWORD is not set", status_code=500)
    if not basic_auth_valid(request.headers.get("authorization", "")):
        return unauthorized_response()
    return await call_next(request)


async def read_upload_bytes(file: UploadFile, label: str = "File") -> bytes:
    content = await file.read()
    if len(content) > config.MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} exceeds {config.MAX_FILE_MB} MB")
    return content


async def extract_pdf_markdown(file: UploadFile, page_range: str = "", source: str = langs.AUTO_SOURCE) -> str:
    content = await read_upload_bytes(file, "PDF")
    return pdf_text.extract_pdf_markdown_from_bytes(content, file.content_type or "application/pdf", page_range, source)


@app.get("/health")
def health():
    ocr_available = bool(shutil.which("pdftoppm") and shutil.which("tesseract"))
    return {
        "status": "ok",
        "version": app.version,
        "fallback_model": config.FALLBACK_MODEL_ID,
        "dedicated_pairs": len(langs.OPUS_PAIRS),
        "device": models.selected_device(),
        # Not a setting any more, but the UI's character counter works from it.
        "max_chars": config.MAX_CHARS,
        "max_file_mb": config.MAX_FILE_MB,
        "ocr_available": ocr_available,
        "ocr_languages": list(ocr.installed_ocr_languages()) if ocr_available else [],
        "model_idle_seconds": config.MODEL_IDLE_SECONDS,
        "cpu_threads": config.CPU_THREADS,
        "model_loaded": models.model_cache_loaded(),
        "root_path": config.ROOT_PATH,
        "timezone": config.HISTORY_TIMEZONE,
        "time_format": config.TIME_FORMAT,
    }


@app.get("/languages")
def languages():
    dedicated_pairs = []
    for key in langs.OPUS_PAIRS:
        source, _, target = key.partition(">")
        if source in langs.CORE_LANGUAGES and target in langs.CORE_LANGUAGES:
            dedicated_pairs.append([langs.CORE_LANGUAGES[source], langs.CORE_LANGUAGES[target]])
    return {
        # The picker starts on auto-detect; DEFAULT_SOURCE is only where failed detection lands.
        "source_default": langs.AUTO_SOURCE,
        "target_default": config.DEFAULT_TARGET,
        "ui_language": config.UI_LANGUAGE,
        "languages": [{"code": code, "name": code} for code in langs.language_codes()],
        "favorites": langs.FAVORITE_LANGUAGES,
        "dedicated_pairs": dedicated_pairs,
        **models.downloaded_state(),
    }


@app.delete("/models/fallback")
def delete_fallback_model():
    models.delete_downloaded_model(config.FALLBACK_MODEL_ID)
    return models.downloaded_state()


@app.delete("/models/{source}/{target}")
def delete_pair_model(source: str, target: str):
    langs.ensure_known_language(source)
    langs.ensure_known_language(target)
    # The id comes from the pair table, never from the request, so nothing outside the model
    # cache can be named.
    entry = langs.OPUS_PAIRS.get(f"{langs.iso_639_1(source)}>{langs.iso_639_1(target)}")
    if not entry:
        raise HTTPException(status_code=404, detail="No dedicated model for this language pair")
    models.delete_downloaded_model(entry["model_id"])
    return models.downloaded_state()


@app.get("/jobs")
def jobs_status():
    jobqueue.ensure_queue_workers()
    return {"workers": config.JOB_WORKERS, "items": jobqueue.list_jobs()}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    return jobqueue.get_job(job_id)


@app.post("/jobs/{job_id}/{action}")
def job_control(job_id: str, action: str):
    return jobqueue.control_job(job_id, action)


@app.post("/jobs/translate")
def start_translate_job(request: TranslateRequest):
    langs.ensure_known_language(request.source, allow_auto=True)
    langs.ensure_known_language(request.target)
    jobqueue.ensure_queue_workers()
    text = "\n\n".join(str(item) for item in request.q) if isinstance(request.q, list) else str(request.q)
    job_id = jobqueue.create_job("translate", request.source, request.target, "Text.txt")
    jobqueue.update_job(job_id, text=text, original_name="text.txt", source_extension="txt")
    jobqueue.register_job_runner(job_id, pipeline.run_text_job, (job_id, text, request.source, request.target, "text.txt", b"", "txt"))
    return {"job_id": job_id}


@app.post("/jobs/translate-file")
async def start_translate_file_job(
    file: UploadFile = File(...),
    text: str = Form(""),
    source: str = Form(config.DEFAULT_SOURCE),
    target: str = Form(config.DEFAULT_TARGET),
    columns: str = Form(""),
    sheet_name: str = Form(""),
    page_range: str = Form(""),
):
    langs.ensure_known_language(source, allow_auto=True)
    langs.ensure_known_language(target)
    jobqueue.ensure_queue_workers()
    content = await read_upload_bytes(file, "Source file")
    filename = file.filename or "source"
    extension = history.file_extension(filename)
    job_id = jobqueue.create_job("translate", source, target, filename)
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    source_payload_path = jobqueue.job_payload_path(job_id).with_suffix(".source")
    source_payload_path.write_bytes(content)
    source_meta = {"columns": columns, "sheet_name": sheet_name, "page_range": page_range}
    jobqueue.update_job(
        job_id,
        text=text,
        original_name=filename,
        source_extension=extension,
        source_payload_path=str(source_payload_path),
        source_meta=source_meta,
    )
    jobqueue.register_job_runner(
        job_id,
        pipeline.run_text_job,
        (job_id, text, source, target, filename, content, extension, source_meta),
    )
    return {"job_id": job_id}


@app.post("/jobs/translate-pdf")
async def start_translate_pdf_job(
    file: UploadFile = File(...),
    source: str = Form(config.DEFAULT_SOURCE),
    target: str = Form(config.DEFAULT_TARGET),
    page_range: str = Form(""),
):
    langs.ensure_known_language(source, allow_auto=True)
    langs.ensure_known_language(target)
    jobqueue.ensure_queue_workers()
    content = await read_upload_bytes(file, "PDF")
    filename = file.filename or "pdf"
    job_id = jobqueue.create_job("translate-pdf", source, target, filename)
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = jobqueue.job_payload_path(job_id)
    payload_path.write_bytes(content)
    jobqueue.update_job(
        job_id,
        payload_path=str(payload_path),
        content_type=file.content_type or "application/pdf",
        filename=filename,
        page_range=page_range,
    )
    jobqueue.register_job_runner(
        job_id,
        pipeline.run_pdf_translate_job,
        (job_id, content, file.content_type or "application/pdf", source, target, filename, page_range),
    )
    return {"job_id": job_id}


@app.post("/jobs/translate-pdf-layout")
async def start_translate_pdf_layout_job(
    file: UploadFile = File(...),
    source: str = Form(config.DEFAULT_SOURCE),
    target: str = Form(config.DEFAULT_TARGET),
    page_range: str = Form(""),
):
    langs.ensure_known_language(source, allow_auto=True)
    langs.ensure_known_language(target)
    jobqueue.ensure_queue_workers()
    content = await read_upload_bytes(file, "PDF")
    filename = file.filename or "pdf"
    job_id = jobqueue.create_job("translate-pdf-layout", source, target, filename)
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = jobqueue.job_payload_path(job_id)
    payload_path.write_bytes(content)
    jobqueue.update_job(job_id, payload_path=str(payload_path), filename=filename, page_range=page_range)
    jobqueue.register_job_runner(
        job_id,
        pipeline.run_pdf_layout_translate_job,
        (job_id, content, source, target, filename, page_range),
    )
    return {"job_id": job_id}


@app.get("/history")
def list_history():
    return {"retention_hours": config.HISTORY_HOURS, "items": history.history_items()}


@app.get("/history/{item_id}")
def download_history(item_id: str):
    path, _ = history.history_paths(item_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    filename = history.history_download_name(history.history_item(item_id, required=False), "md")
    return FileResponse(path, media_type="text/markdown", filename=filename)


@app.get("/history/{item_id}/export")
def export_history(item_id: str, format: str = "md"):
    path, _ = history.history_paths(item_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    text = path.read_text(encoding="utf-8")
    safe_format = format.lower()
    item = history.history_item(item_id, required=False)
    filename_base = history.history_download_name(item, "").rstrip(".")
    if safe_format == "md":
        return FileResponse(path, media_type="text/markdown", filename=f"{filename_base}.md")
    if safe_format == "txt":
        return Response(
            text,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.txt"'},
        )
    if safe_format == "pdf":
        return Response(
            textdocs.create_text_pdf(text),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'},
        )
    if safe_format == "doc":
        return Response(
            textdocs.create_text_docx(text),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.docx"'},
        )
    source_extension = history.file_extension("x." + item.get("source_extension", ""))
    requested_extension = source_extension if safe_format == "original" else history.file_extension("x." + safe_format)
    source_path = history.history_source_path(item_id, source_extension) if source_extension else None
    if not source_path or not source_path.exists():
        raise HTTPException(status_code=404, detail="History source file not found")
    if requested_extension != source_extension:
        raise HTTPException(status_code=400, detail="History source can only be exported in its original format")
    source_meta = item.get("source_meta", {})
    content = history.history_original_export(
        item_id,
        source_extension,
        source_path.read_bytes(),
        text,
        source_meta,
    )
    return Response(
        content,
        media_type=history.original_export_media_type(source_extension),
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.{source_extension}"'},
    )


@app.post("/export-pdf")
def export_pdf(request: PdfExportRequest):
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="No text to export")
    return Response(
        textdocs.create_text_pdf(request.text),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.pdf"'},
    )


@app.post("/export-doc")
def export_doc(request: DocxExportRequest):
    """A fresh DOCX built from plain translated text, for sources with no original DOCX
    structure to preserve (see create_text_docx). Distinct from /export-docx, which
    reinjects translated text into an uploaded original DOCX."""
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="No text to export")
    return Response(
        textdocs.create_text_docx(request.text),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.docx"'},
    )


@app.post("/export-docx")
async def export_docx(
    file: UploadFile = File(...),
    text: str = Form(""),
):
    content = await read_upload_bytes(file, "DOCX")
    return Response(
        formats.export_docx_with_translated_text(content, text),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.docx"'},
    )


@app.post("/export-odt")
async def export_odt(
    file: UploadFile = File(...),
    text: str = Form(""),
):
    content = await read_upload_bytes(file, "ODT")
    return Response(
        formats.export_odt_with_translated_text(content, text),
        media_type="application/vnd.oasis.opendocument.text",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.odt"'},
    )


@app.post("/export-pptx")
async def export_pptx(
    file: UploadFile = File(...),
    text: str = Form(""),
):
    content = await read_upload_bytes(file, "PPTX")
    return Response(
        formats.export_pptx_with_translated_text(content, text),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.pptx"'},
    )


@app.post("/export-csv")
async def export_csv(
    file: UploadFile = File(...),
    text: str = Form(""),
    columns: str = Form(""),
):
    content = await read_upload_bytes(file, "CSV")
    exported = (
        formats.export_csv_auto_with_translated_text(content, text)
        if not columns.strip()
        else formats.export_csv_with_translated_text(content, columns, text)
    )
    return Response(
        exported,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.csv"'},
    )


@app.post("/export-xlsx")
async def export_xlsx(
    file: UploadFile = File(...),
    text: str = Form(""),
    sheet_name: str = Form(""),
    columns: str = Form(""),
):
    content = await read_upload_bytes(file, "XLSX")
    if not columns.strip():
        exported = formats.export_xlsx_auto_with_translated_text(content, text)
    elif formats.xlsx_is_multi_sheet_spec(columns):
        exported = formats.export_xlsx_multi_sheet_with_translated_text(content, columns, text)
    else:
        exported = formats.export_xlsx_with_translated_text(content, sheet_name, columns, text)
    return Response(
        exported,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.xlsx"'},
    )


@app.post("/export-html")
async def export_html(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "HTML")
    return Response(formats.export_html_with_translated_text(content, text), media_type="text/html")


@app.post("/export-subtitle")
async def export_subtitle(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "Subtitle")
    return Response(formats.export_subtitle_with_translated_text(content, text), media_type="text/plain")


@app.post("/export-json")
async def export_json(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "JSON")
    return Response(formats.export_json_with_translated_text(content, text), media_type="application/json")


@app.post("/export-yaml")
async def export_yaml(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "YAML")
    return Response(formats.export_yaml_with_translated_text(content, text), media_type="text/yaml")


@app.post("/export-po")
async def export_po(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "PO")
    return Response(formats.export_po_with_translated_text(content, text), media_type="text/plain")


@app.post("/export-xliff")
async def export_xliff(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "XLIFF")
    return Response(formats.export_xliff_with_translated_text(content, text), media_type="application/xml")


@app.delete("/history/{item_id}")
def delete_history(item_id: str):
    md_path, json_path = history.history_paths(item_id)
    if not md_path.exists() and not json_path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    source_path = None
    export_path = None
    if json_path.exists():
        try:
            item = json.loads(json_path.read_text(encoding="utf-8"))
            source_extension = history.file_extension("x." + item.get("source_extension", ""))
            if source_extension:
                source_path = history.history_source_path(item_id, source_extension)
                export_path = history.history_export_path(item_id, source_extension)
        except Exception:
            source_path = None
    md_path.unlink(missing_ok=True)
    json_path.unlink(missing_ok=True)
    if source_path:
        source_path.unlink(missing_ok=True)
    if export_path:
        export_path.unlink(missing_ok=True)
    return {"deleted": item_id}


@app.delete("/history")
def reset_history(keep: int = 0):
    """Delete every history entry except the `keep` most recent ones.

    history_items() already sorts newest first, so the cut is just a slice; each item past it
    goes through delete_history for its source file and both stored paths, not a bare unlink.
    """
    keep = max(0, keep)
    items = history.history_items()
    deleted = 0
    for item in items[keep:]:
        try:
            delete_history(item["id"])
            deleted += 1
        except HTTPException as exc:
            # A concurrent delete of the same item already reached the goal state (gone), so
            # this is not a reason to abort the rest of the batch.
            if exc.status_code != 404:
                raise
    return {"deleted": deleted, "kept": len(items) - deleted}


@app.get("/")
def index():
    return FileResponse(config.APP_DIR / "templates" / "index.html", media_type="text/html")


@app.post("/translate")
def translate(request: TranslateRequest):
    if isinstance(request.q, list):
        translated = [translation.translate_text(str(item), request.source, request.target) for item in request.q]
    else:
        translated = translation.translate_text(str(request.q), request.source, request.target)
    return {"translatedText": translated}


@app.post("/extract-pdf", response_class=PlainTextResponse)
async def extract_pdf(
    file: UploadFile = File(...),
    page_range: str = Form(""),
    source: str = Form(langs.AUTO_SOURCE),
):
    return await extract_pdf_markdown(file, page_range, source)


@app.post("/extract-docx", response_class=PlainTextResponse)
async def extract_docx(file: UploadFile = File(...), page_range: str = Form("")):
    content = await read_upload_bytes(file, "DOCX")
    return formats.extract_docx_text_from_bytes(content, page_range)


@app.post("/extract-odt", response_class=PlainTextResponse)
async def extract_odt(file: UploadFile = File(...), page_range: str = Form("")):
    content = await read_upload_bytes(file, "ODT")
    return formats.extract_odt_text_from_bytes(content, page_range)


@app.post("/extract-pptx", response_class=PlainTextResponse)
async def extract_pptx(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "PPTX")
    return formats.extract_pptx_text_from_bytes(content)


@app.post("/extract-csv", response_class=PlainTextResponse)
async def extract_csv(file: UploadFile = File(...), columns: str = Form("")):
    content = await read_upload_bytes(file, "CSV")
    if not columns.strip():
        return formats.extract_csv_auto_text_from_bytes(content)
    return formats.extract_csv_text_from_bytes(content, columns)


@app.post("/extract-xlsx", response_class=PlainTextResponse)
async def extract_xlsx(
    file: UploadFile = File(...),
    sheet_name: str = Form(""),
    columns: str = Form(""),
):
    content = await read_upload_bytes(file, "XLSX")
    if not columns.strip():
        return formats.extract_xlsx_auto_text_from_bytes(content)
    if formats.xlsx_is_multi_sheet_spec(columns):
        return formats.extract_xlsx_multi_sheet_text_from_bytes(content, columns)
    return formats.extract_xlsx_text_from_bytes(content, sheet_name, columns)


@app.post("/extract-html", response_class=PlainTextResponse)
async def extract_html(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "HTML")
    return formats.extract_html_text_from_bytes(content)


@app.post("/extract-subtitle", response_class=PlainTextResponse)
async def extract_subtitle(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "Subtitle")
    return formats.extract_subtitle_text_from_bytes(content)


@app.post("/extract-json", response_class=PlainTextResponse)
async def extract_json(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "JSON")
    return formats.extract_json_text_from_bytes(content)


@app.post("/extract-yaml", response_class=PlainTextResponse)
async def extract_yaml(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "YAML")
    return formats.extract_yaml_text_from_bytes(content)


@app.post("/extract-po", response_class=PlainTextResponse)
async def extract_po(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "PO")
    return formats.extract_po_text_from_bytes(content)


@app.post("/extract-xliff", response_class=PlainTextResponse)
async def extract_xliff(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "XLIFF")
    return formats.extract_xliff_text_from_bytes(content)


@app.post("/translate-pdf", response_class=PlainTextResponse)
async def translate_pdf(
    file: UploadFile = File(...),
    source: str = Form(config.DEFAULT_SOURCE),
    target: str = Form(config.DEFAULT_TARGET),
    page_range: str = Form(""),
):
    markdown = await extract_pdf_markdown(file, page_range, source)
    sections = pipeline.merge_cross_page_sentences(pipeline.pdf_sections(markdown))
    translated = [f"# Page {page_number}\n\n{translation.translate_text(page_text, source, target)}"
                  for page_number, page_text in sections]
    return "\n\n".join(translated)
