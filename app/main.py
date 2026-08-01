import os
import re
import csv
import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Dict, List, Union
from xml.etree import ElementTree

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer


MODEL_ID = os.getenv("NLLB_MODEL", "facebook/nllb-200-distilled-600M")
DEVICE_SETTING = os.getenv("NLLB_DEVICE", "cpu")
MAX_CHARS = int(os.getenv("NLLB_MAX_CHARS", "6000"))
MAX_FILE_MB = int(os.getenv("NLLB_MAX_FILE_MB", "50"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
HISTORY_DAYS = int(os.getenv("NLLB_HISTORY_DAYS", "7"))
HISTORY_DIR = Path(os.getenv("NLLB_HISTORY_DIR", "/data/history"))
DEFAULT_SOURCE = os.getenv("NLLB_DEFAULT_SOURCE", "eng_Latn")
DEFAULT_TARGET = os.getenv("NLLB_DEFAULT_TARGET", "deu_Latn")
OCR_ENABLED = os.getenv("NLLB_ENABLE_OCR", "false").lower() in ("1", "true", "yes", "on")
OCR_LANGUAGE = os.getenv("NLLB_OCR_LANGUAGE", "deu+eng")
PDF_LOW_TEXT_CHARS = 20

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="NLLB Translate", version="0.1.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


class TranslateRequest(BaseModel):
    q: Union[str, List[str]]
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET


def selected_device():
    if DEVICE_SETTING == "cuda" and torch.cuda.is_available():
        return "cuda"
    return "cpu"


@lru_cache(maxsize=1)
def load_tokenizer():
    return AutoTokenizer.from_pretrained(MODEL_ID)


@lru_cache(maxsize=1)
def load_model():
    device = selected_device()
    tokenizer = load_tokenizer()
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_ID)
    model.to(device)
    model.eval()
    return tokenizer, model, device


def language_codes():
    tokenizer = load_tokenizer()
    codes = getattr(tokenizer, "additional_special_tokens", [])
    return sorted(
        code for code in codes
        if "_" in code and len(code.split("_", 1)[0]) == 3
    )


def translate_one(text: str, source: str, target: str) -> str:
    if not text:
        return ""

    tokenizer, model, device = load_model()
    tokenizer.src_lang = source
    inputs = tokenizer(text, return_tensors="pt", truncation=True).to(device)
    forced_bos_token_id = tokenizer.convert_tokens_to_ids(target)

    with torch.inference_mode():
        generated = model.generate(
            **inputs,
            forced_bos_token_id=forced_bos_token_id,
            max_new_tokens=1024,
            num_beams=4,
        )
    return tokenizer.batch_decode(generated, skip_special_tokens=True)[0]


def split_long_text(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break

        window = remaining[:max_chars]
        cut = max(
            window.rfind("\n\n"),
            window.rfind(". "),
            window.rfind("! "),
            window.rfind("? "),
            window.rfind("; "),
            window.rfind(": "),
        )
        if cut < max_chars * 0.5:
            whitespace = [match.start() for match in re.finditer(r"\s+", window)]
            cut = whitespace[-1] if whitespace else max_chars
        else:
            cut += 1

        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return chunks


def translate_text(text: str, source: str, target: str) -> str:
    chunks = split_long_text(text, MAX_CHARS)
    translated = [translate_one(chunk, source, target) for chunk in chunks]
    return "\n\n".join(translated)


def cleanup_history():
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - (HISTORY_DAYS * 86400)
    for path in HISTORY_DIR.glob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def history_safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")
    return safe[:80] or "translation"


def save_history(kind: str, result: str, source: str, target: str, original_name: str = "translation") -> str:
    cleanup_history()
    item_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    base = f"{created_at[:10]}-{history_safe_name(original_name)}-{item_id[:8]}"
    md_path = HISTORY_DIR / f"{base}.md"
    json_path = HISTORY_DIR / f"{base}.json"
    md_path.write_text(result, encoding="utf-8")
    json_path.write_text(json.dumps({
        "id": base,
        "kind": kind,
        "source": source,
        "target": target,
        "created_at": created_at,
        "filename": md_path.name,
    }), encoding="utf-8")
    return base


def history_items():
    cleanup_history()
    items = []
    for meta_path in sorted(HISTORY_DIR.glob("*.json"), reverse=True):
        try:
            item = json.loads(meta_path.read_text(encoding="utf-8"))
            md_path = HISTORY_DIR / f"{item['id']}.md"
            item["size_bytes"] = md_path.stat().st_size if md_path.exists() else 0
            items.append(item)
        except Exception:
            continue
    return items


def history_paths(item_id: str):
    if ".." in item_id or "/" in item_id or "\\" in item_id:
        raise HTTPException(status_code=400, detail="Invalid history id")
    return HISTORY_DIR / f"{item_id}.md", HISTORY_DIR / f"{item_id}.json"


async def read_upload_bytes(file: UploadFile, label: str = "File") -> bytes:
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} exceeds {MAX_FILE_MB} MB")
    return content


def create_job(kind: str) -> str:
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "message": "Queued",
            "current": 0,
            "total": 0,
            "percent": 0,
            "eta_seconds": None,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "pause_requested": False,
            "cancel_requested": False,
        }
    return job_id


def update_job(job_id: str, **values):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.update(values)
        current = job.get("current") or 0
        total = job.get("total") or 0
        started_at = job.get("started_at")
        if total:
            job["percent"] = round((current / total) * 100, 1)
        if started_at and current and total and current < total:
            elapsed = time.time() - started_at
            job["eta_seconds"] = round((elapsed / current) * (total - current))
        elif current and total and current >= total:
            job["eta_seconds"] = 0


def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return dict(job)


def control_job(job_id: str, action: str):
    if action == "pause":
        update_job(job_id, pause_requested=True, message="Pause requested")
    elif action == "resume":
        update_job(job_id, pause_requested=False, message="Resuming")
    elif action == "cancel":
        update_job(job_id, cancel_requested=True, pause_requested=False, message="Stop requested")
    else:
        raise HTTPException(status_code=400, detail="Unknown job action")
    return get_job(job_id)


def wait_if_paused_or_cancelled(job_id: str):
    while True:
        job = get_job(job_id)
        if job.get("cancel_requested"):
            raise RuntimeError("Job stopped by user")
        if not job.get("pause_requested"):
            return
        update_job(job_id, status="paused", message="Paused")
        time.sleep(1)


def translate_chunks(chunks: List[str], source: str, target: str, job_id: str, offset: int = 0) -> List[str]:
    translated = []
    for index, chunk in enumerate(chunks, start=1):
        wait_if_paused_or_cancelled(job_id)
        update_job(
            job_id,
            status="running",
            current=offset + index - 1,
            message=f"Translating chunk {offset + index}",
        )
        translated.append(translate_one(chunk, source, target))
        update_job(job_id, current=offset + index)
    return translated


def parse_page_range(page_range: str, total_pages: int) -> List[int]:
    if not page_range.strip():
        return list(range(1, total_pages + 1))

    pages = set()
    for part in page_range.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                if not start_text.strip() or not end_text.strip():
                    raise ValueError
                start = int(start_text)
                end = int(end_text)
                if start > end:
                    raise ValueError
                pages.update(range(start, end + 1))
            else:
                pages.add(int(part))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid page range") from exc

    selected = sorted(pages)
    if not selected or selected[0] < 1 or selected[-1] > total_pages:
        raise HTTPException(status_code=400, detail=f"Page range must be between 1 and {total_pages}")
    return selected


def ensure_ocr_tools():
    missing = [tool for tool in ("pdftoppm", "tesseract") if not shutil.which(tool)]
    if missing:
        raise HTTPException(status_code=500, detail="OCR tools missing in image: " + ", ".join(missing))


def ocr_pdf_page(content: bytes, page_number: int) -> str:
    ensure_ocr_tools()
    with tempfile.TemporaryDirectory() as temp_dir:
        pdf_path = Path(temp_dir) / "input.pdf"
        output_prefix = Path(temp_dir) / "page"
        pdf_path.write_bytes(content)
        subprocess.run(
            [
                "pdftoppm",
                "-f",
                str(page_number),
                "-l",
                str(page_number),
                "-png",
                "-singlefile",
                str(pdf_path),
                str(output_prefix),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        image_path = output_prefix.with_suffix(".png")
        result = subprocess.run(
            ["tesseract", str(image_path), "stdout", "-l", OCR_LANGUAGE],
            check=True,
            capture_output=True,
            text=True,
        )
    return result.stdout.strip()


def extract_pdf_markdown_from_bytes(
    content: bytes,
    content_type: str = "application/pdf",
    page_range: str = "",
) -> str:
    if content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")

    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"PDF exceeds {MAX_FILE_MB} MB")

    try:
        reader = PdfReader(BytesIO(content))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}") from exc

    if not reader.pages:
        raise HTTPException(status_code=422, detail="PDF has no pages")

    selected_pages = parse_page_range(page_range, len(reader.pages))
    pages = []
    pages_with_text = 0
    for index in selected_pages:
        page = reader.pages[index - 1]
        text = page.extract_text() or ""
        text = re.sub(r"[ \t]+\n", "\n", text).strip()
        needs_ocr = not text or len(text) < PDF_LOW_TEXT_CHARS
        if needs_ocr and OCR_ENABLED:
            ocr_text = ocr_pdf_page(content, index)
            if ocr_text:
                text = ocr_text

        if text:
            pages_with_text += 1
            if len(text) < PDF_LOW_TEXT_CHARS and not OCR_ENABLED:
                text += "\n\n> Warning: This page has very little extractable text and may need OCR."
            pages.append(f"# Page {index}\n\n{text}".strip())
        else:
            pages.append(f"# Page {index}\n\n> No extractable text found on this page. It may need OCR.")

    markdown = "\n\n".join(pages).strip()
    if not pages_with_text:
        ocr_detail = (
            f"OCR is configured for {OCR_LANGUAGE}, but no readable text was produced."
            if OCR_ENABLED
            else "OCR is disabled. Set NLLB_ENABLE_OCR=true to use OCR fallback."
        )
        raise HTTPException(
            status_code=422,
            detail=f"No extractable text found. This PDF may be scanned, image-only, or protected. {ocr_detail}",
        )
    return markdown


def exception_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def extract_docx_text_from_bytes(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as docx:
            document = docx.read("word/document.xml")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read DOCX: {exc}") from exc

    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse DOCX XML: {exc}") from exc

    paragraphs = []
    for paragraph in root.findall(".//w:p", namespaces):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", namespaces)]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)

    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in DOCX")
    return result


def extract_odt_text_from_bytes(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as odt:
            document = odt.read("content.xml")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read ODT: {exc}") from exc

    namespaces = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse ODT XML: {exc}") from exc

    paragraphs = []
    for paragraph in root.findall(".//text:p", namespaces):
        text = "".join(paragraph.itertext()).strip()
        if text:
            paragraphs.append(text)

    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in ODT")
    return result


def parse_column_names(columns: str) -> List[str]:
    return [column.strip() for column in columns.split(",") if column.strip()]


def extract_csv_text_from_bytes(content: bytes, columns: str) -> str:
    selected_columns = parse_column_names(columns)
    if not selected_columns:
        raise HTTPException(status_code=400, detail="Select at least one CSV column")

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode CSV as UTF-8: {exc}") from exc

    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=422, detail="CSV has no header row")

    missing = [column for column in selected_columns if column not in reader.fieldnames]
    if missing:
        raise HTTPException(status_code=400, detail="CSV columns not found: " + ", ".join(missing))

    rows = []
    for row in reader:
        values = [row.get(column, "").strip() for column in selected_columns]
        line = " | ".join(value for value in values if value)
        if line:
            rows.append(line)

    result = "\n\n".join(rows).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in selected CSV columns")
    return result


def xlsx_column_name(cell_ref: str) -> str:
    return re.sub(r"[^A-Z]", "", cell_ref.upper())


def xlsx_shared_strings(workbook: zipfile.ZipFile) -> List[str]:
    try:
        xml = workbook.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(xml)
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values = []
    for item in root.findall(".//s:si", namespace):
        values.append("".join(item.itertext()))
    return values


def xlsx_relationship_target(target: str) -> str:
    normalized = target.replace("\\", "/").lstrip("/")
    if normalized.startswith("../") or "/../" in normalized:
        raise HTTPException(status_code=400, detail="Invalid XLSX relationship target")
    if normalized.startswith("xl/"):
        return normalized
    return "xl/" + normalized


def xlsx_sheet_path(workbook: zipfile.ZipFile, sheet_name: str) -> str:
    namespace = {
        "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }
    rel_namespace = {"rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    workbook_xml = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
    rels_xml = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    rels = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_xml.findall(".//rel:Relationship", rel_namespace)
    }
    sheets = workbook_xml.findall(".//m:sheet", namespace)
    if not sheets:
        raise HTTPException(status_code=422, detail="XLSX has no sheets")

    selected = None
    if sheet_name.strip():
        for sheet in sheets:
            if sheet.attrib.get("name") == sheet_name.strip():
                selected = sheet
                break
        if selected is None:
            raise HTTPException(status_code=400, detail=f"XLSX sheet not found: {sheet_name}")
    else:
        selected = sheets[0]

    target = rels.get(selected.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"])
    if not target:
        raise HTTPException(status_code=400, detail="Could not resolve XLSX sheet")
    return xlsx_relationship_target(target)


def extract_xlsx_text_from_bytes(content: bytes, sheet_name: str, columns: str) -> str:
    selected_columns = parse_column_names(columns)
    if not selected_columns:
        raise HTTPException(status_code=400, detail="Select at least one XLSX column")

    try:
        workbook = zipfile.ZipFile(BytesIO(content))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read XLSX: {exc}") from exc

    try:
        with workbook:
            shared = xlsx_shared_strings(workbook)
            sheet_path = xlsx_sheet_path(workbook, sheet_name)
            root = ElementTree.fromstring(workbook.read(sheet_path))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse XLSX: {exc}") from exc

    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    for row in root.findall(".//s:row", namespace):
        values = {}
        for cell in row.findall("s:c", namespace):
            ref = cell.attrib.get("r", "")
            column = xlsx_column_name(ref)
            cell_type = cell.attrib.get("t")
            value_node = cell.find("s:v", namespace)
            inline_node = cell.find(".//s:t", namespace)
            value = ""
            if cell_type == "s" and value_node is not None:
                try:
                    value = shared[int(value_node.text or "0")]
                except (ValueError, IndexError) as exc:
                    raise HTTPException(status_code=400, detail="Invalid XLSX shared string reference") from exc
            elif inline_node is not None:
                value = inline_node.text or ""
            elif value_node is not None:
                value = value_node.text or ""
            values[column] = value.strip()
        if values:
            rows.append(values)

    if not rows:
        raise HTTPException(status_code=422, detail="XLSX sheet has no rows")

    header = {value: column for column, value in rows[0].items() if value}
    resolved_columns = [header.get(column, column.upper()) for column in selected_columns]
    missing = [column for column in resolved_columns if all(not row.get(column) for row in rows)]
    if missing:
        raise HTTPException(status_code=400, detail="XLSX columns not found: " + ", ".join(missing))

    output_rows = []
    for row in rows[1:]:
        values = [row.get(column, "").strip() for column in resolved_columns]
        line = " | ".join(value for value in values if value)
        if line:
            output_rows.append(line)

    result = "\n\n".join(output_rows).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in selected XLSX columns")
    return result


async def extract_pdf_markdown(file: UploadFile, page_range: str = "") -> str:
    content = await read_upload_bytes(file, "PDF")
    return extract_pdf_markdown_from_bytes(content, file.content_type or "application/pdf", page_range)


def run_text_job(job_id: str, text: str, source: str, target: str):
    try:
        chunks = split_long_text(text, MAX_CHARS)
        update_job(
            job_id,
            status="running",
            message="Model loading or translation running",
            total=len(chunks),
            started_at=time.time(),
        )
        result = "\n\n".join(translate_chunks(chunks, source, target, job_id))
        history_id = save_history("text", result, source, target, "text")
        update_job(
            job_id,
            status="complete",
            message="Complete",
            current=len(chunks),
            result=result,
            history_id=history_id,
            finished_at=time.time(),
        )
    except Exception as exc:
        update_job(job_id, status="failed", message="Failed", error=exception_message(exc), finished_at=time.time())


def pdf_sections(markdown: str):
    sections = []
    for section in re.split(r"(?m)^# Page ", markdown):
        section = section.strip()
        if not section:
            continue
        page_number, _, page_text = section.partition("\n")
        sections.append((page_number.strip(), page_text.strip()))
    return sections


def run_pdf_translate_job(
    job_id: str,
    content: bytes,
    content_type: str,
    source: str,
    target: str,
    filename: str,
    page_range: str = "",
):
    try:
        update_job(job_id, status="running", message="Extracting PDF", started_at=time.time())
        markdown = extract_pdf_markdown_from_bytes(content, content_type, page_range)
        sections = pdf_sections(markdown)
        planned = [(page_number, split_long_text(page_text, MAX_CHARS)) for page_number, page_text in sections]
        total = sum(len(chunks) for _, chunks in planned)
        update_job(job_id, total=total, message=f"Translating 0 / {total} chunks")

        current = 0
        pages = []
        for page_number, chunks in planned:
            translated_chunks = translate_chunks(chunks, source, target, job_id, current)
            current += len(chunks)
            update_job(job_id, message=f"Translated page {page_number}")
            pages.append(f"# Page {page_number}\n\n" + "\n\n".join(translated_chunks))

        result = "\n\n".join(pages)
        history_id = save_history("pdf", result, source, target, filename)
        update_job(
            job_id,
            status="complete",
            message="Complete",
            current=total,
            result=result,
            history_id=history_id,
            finished_at=time.time(),
        )
    except Exception as exc:
        update_job(job_id, status="failed", message="Failed", error=exception_message(exc), finished_at=time.time())


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL_ID,
        "device": selected_device(),
        "max_chars": MAX_CHARS,
        "max_file_mb": MAX_FILE_MB,
        "ocr_enabled": OCR_ENABLED,
        "ocr_language": OCR_LANGUAGE,
    }


@app.get("/languages")
def languages():
    return {
        "source_default": DEFAULT_SOURCE,
        "target_default": DEFAULT_TARGET,
        "languages": [{"code": code, "name": code} for code in language_codes()],
    }


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    return get_job(job_id)


@app.post("/jobs/{job_id}/{action}")
def job_control(job_id: str, action: str):
    return control_job(job_id, action)


@app.post("/jobs/translate")
def start_translate_job(request: TranslateRequest):
    job_id = create_job("translate")
    text = "\n\n".join(str(item) for item in request.q) if isinstance(request.q, list) else str(request.q)
    thread = threading.Thread(
        target=run_text_job,
        args=(job_id, text, request.source, request.target),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.post("/jobs/translate-pdf")
async def start_translate_pdf_job(
    file: UploadFile = File(...),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
    page_range: str = Form(""),
):
    content = await read_upload_bytes(file, "PDF")
    job_id = create_job("translate-pdf")
    thread = threading.Thread(
        target=run_pdf_translate_job,
        args=(job_id, content, file.content_type or "application/pdf", source, target, file.filename or "pdf", page_range),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.get("/history")
def list_history():
    return {"retention_days": HISTORY_DAYS, "items": history_items()}


@app.get("/history/{item_id}")
def download_history(item_id: str):
    path, _ = history_paths(item_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    return FileResponse(path, media_type="text/markdown", filename=path.name)


@app.delete("/history/{item_id}")
def delete_history(item_id: str):
    md_path, json_path = history_paths(item_id)
    if not md_path.exists() and not json_path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    md_path.unlink(missing_ok=True)
    json_path.unlink(missing_ok=True)
    return {"deleted": item_id}


@app.get("/")
def index():
    return FileResponse(APP_DIR / "templates" / "index.html", media_type="text/html")


@app.post("/translate")
def translate(request: TranslateRequest):
    if isinstance(request.q, list):
        translated = [translate_text(str(item), request.source, request.target) for item in request.q]
    else:
        translated = translate_text(str(request.q), request.source, request.target)
    return {"translatedText": translated}


@app.post("/extract-pdf", response_class=PlainTextResponse)
async def extract_pdf(file: UploadFile = File(...), page_range: str = Form("")):
    return await extract_pdf_markdown(file, page_range)


@app.post("/extract-docx", response_class=PlainTextResponse)
async def extract_docx(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "DOCX")
    return extract_docx_text_from_bytes(content)


@app.post("/extract-odt", response_class=PlainTextResponse)
async def extract_odt(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "ODT")
    return extract_odt_text_from_bytes(content)


@app.post("/extract-csv", response_class=PlainTextResponse)
async def extract_csv(file: UploadFile = File(...), columns: str = Form("")):
    content = await read_upload_bytes(file, "CSV")
    return extract_csv_text_from_bytes(content, columns)


@app.post("/extract-xlsx", response_class=PlainTextResponse)
async def extract_xlsx(
    file: UploadFile = File(...),
    sheet_name: str = Form(""),
    columns: str = Form(""),
):
    content = await read_upload_bytes(file, "XLSX")
    return extract_xlsx_text_from_bytes(content, sheet_name, columns)


@app.post("/translate-pdf", response_class=PlainTextResponse)
async def translate_pdf(
    file: UploadFile = File(...),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
    page_range: str = Form(""),
):
    markdown = await extract_pdf_markdown(file, page_range)
    translated = []
    for section in re.split(r"(?m)^# Page ", markdown):
        section = section.strip()
        if not section:
            continue
        page_number, _, page_text = section.partition("\n")
        translated.append(f"# Page {page_number.strip()}\n\n{translate_text(page_text.strip(), source, target)}")
    return "\n\n".join(translated)

