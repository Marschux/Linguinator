"""The translation history on disk, its cleanup and the original-format re-export."""
import os
import re
import json
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import HTTPException
from . import config, langs, textdocs, pdf_layout, formats, pdf_scan


def cleanup_history():
    """Delete history entries past their retention time, whole.

    Per entry, not per file: an entry is a metadata file plus the translation, the retained source
    and, once someone downloads it, the prepared original-format export. That export is written
    later than the rest, so going by each file's own age would have left the translated document
    on disk after everything else about it was gone.

    Files whose metadata is already missing are still dropped by their own age, which is how a
    half-deleted entry from an earlier pass finally disappears.
    """
    config.HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    if config.HISTORY_HOURS is None:
        return
    cutoff = time.time() - (config.HISTORY_HOURS * 3600)
    expired = {path.name[:-len(".json")] for path in config.HISTORY_DIR.glob("*.json")
               if path.stat().st_mtime < cutoff}
    # Entries x files per pass, fine at the few hundred a retention window holds.
    for path in config.HISTORY_DIR.iterdir():
        if not path.is_file():
            continue
        belongs_to_expired = any(path.name.startswith(base + ".") for base in expired)
        if belongs_to_expired or path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def history_safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")
    return safe[:80] or "translation"


def file_extension(name: str) -> str:
    extension = Path(name or "").suffix.lower().lstrip(".")
    return re.sub(r"[^a-z0-9]+", "", extension)[:12]


def history_source_path(item_id: str, extension: str) -> Path:
    safe_extension = file_extension("x." + extension)
    return config.HISTORY_DIR / f"{item_id}.source.{safe_extension}"


def history_export_path(item_id: str, extension: str) -> Path:
    """Where the finished original-format export is kept.

    Rebuilding it per download meant waiting seconds for the button to do anything: a
    layout-preserving PDF is re-extracted, reflowed and re-rendered from scratch, measured at 2.8 s
    for a 12-page document against 2 ms for the plain-text formats. It only ever produces the same
    bytes, so it is produced once - by the job that created the entry, or by the first download of
    an older one - and served from disk afterwards.
    """
    history_paths(item_id)  # rejects an id that would point outside the history directory
    safe_extension = file_extension("x." + extension)
    return config.HISTORY_DIR / f"{item_id}.export.{safe_extension}"


def history_local_time(created_at: str) -> Optional[datetime]:
    """A history item's timestamp in LINGUINATOR_TIMEZONE, or None if it cannot be read.

    Falls back to UTC when the zone is unknown: some Python installs ship without a tz database,
    and a download must not fail over the time in its name.
    """
    try:
        moment = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None
    try:
        return moment.astimezone(ZoneInfo(config.HISTORY_TIMEZONE))
    except Exception:
        return moment


def history_download_name(item: Dict[str, Any], extension: str) -> str:
    """`<original name>_<date>_<time>_<target>.<ext>`.

    The stored name is an internal id (date, sanitised name, random code). Downloading several
    languages or several runs of one document then gives files nothing tells apart.
    """
    # Decomposed first, so "Geschäftsbedingungen" becomes "Geschaftsbedingungen" rather than the
    # "Gesch-ftsbedingungen" a plain non-ASCII purge leaves behind. The header stays ASCII, which
    # keeps Content-Disposition free of RFC 5987 encoding.
    original = unicodedata.normalize("NFKD", item.get("original_name") or "translation")
    stem = Path(original.encode("ascii", "ignore").decode("ascii")).stem or "translation"
    moment = history_local_time(item.get("created_at", ""))
    stamp = moment.strftime("%Y-%m-%d_%H%M") if moment else "undated"
    # Just the language part: "deu_Latn" says everything "deu" does for a file name.
    target = (item.get("target") or "").split("_", 1)[0] or "translated"
    return f"{history_safe_name(f'{stem}_{stamp}_{target}')}.{extension}"


def save_history(
    kind: str,
    result: str,
    source: str,
    target: str,
    original_name: str = "translation",
    source_content: bytes = b"",
    source_extension: str = "",
    source_meta: Optional[Dict[str, str]] = None,
) -> str:
    cleanup_history()
    item_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    base = f"{created_at[:10]}-{history_safe_name(original_name)}-{item_id[:8]}"
    md_path = config.HISTORY_DIR / f"{base}.md"
    json_path = config.HISTORY_DIR / f"{base}.json"
    source_ext = file_extension("x." + source_extension) or file_extension(original_name)
    md_path.write_text(result, encoding="utf-8")
    metadata = {
        "id": base,
        "kind": kind,
        "source": source,
        "target": target,
        "created_at": created_at,
        "filename": md_path.name,
        "original_name": original_name,
        "code": item_id[:8],
        "source_extension": source_ext,
        "source_meta": source_meta or {},
    }
    if source_content and source_ext:
        source_path = history_source_path(base, source_ext)
        source_path.write_bytes(source_content)
        metadata["source_filename"] = source_path.name
    json_path.write_text(json.dumps(metadata), encoding="utf-8")
    return base


def history_items():
    cleanup_history()
    items = []
    for meta_path in config.HISTORY_DIR.glob("*.json"):
        try:
            item = json.loads(meta_path.read_text(encoding="utf-8"))
            md_path = config.HISTORY_DIR / f"{item['id']}.md"
            source_ext = item.get("source_extension", "")
            source_file = history_source_path(item["id"], source_ext) if source_ext else None
            item["size_bytes"] = md_path.stat().st_size if md_path.exists() else 0
            item["has_source_file"] = bool(source_file and source_file.exists())
            items.append(item)
        except Exception:
            continue
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return items


def history_paths(item_id: str):
    if ".." in item_id or "/" in item_id or "\\" in item_id:
        raise HTTPException(status_code=400, detail="Invalid history id")
    return config.HISTORY_DIR / f"{item_id}.md", config.HISTORY_DIR / f"{item_id}.json"


def history_item(item_id: str, required: bool = True) -> Dict[str, Any]:
    _, json_path = history_paths(item_id)
    if not json_path.exists():
        if not required:
            # cleanup_history deletes files one by one, so the metadata can already be gone while
            # the result is still there. A download then falls back to a generic name, not a 404.
            return {}
        raise HTTPException(status_code=404, detail="History item not found")
    return json.loads(json_path.read_text(encoding="utf-8"))


def original_export_media_type(extension: str) -> str:
    return {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "odt": "application/vnd.oasis.opendocument.text",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "html": "text/html",
        "htm": "text/html",
        "srt": "text/plain",
        "vtt": "text/plain",
        "json": "application/json",
        "yaml": "text/yaml",
        "yml": "text/yaml",
        "po": "text/plain",
        "xlf": "application/xml",
        "xliff": "application/xml",
        "pdf": "application/pdf",
        "md": "text/markdown",
        "txt": "text/plain",
    }.get(extension, "application/octet-stream")


def export_original_history_content(extension: str, content: bytes, text: str, source_meta: Dict[str, str]) -> bytes:
    if extension == "docx":
        return formats.export_docx_with_translated_text(content, text, source_meta.get("page_range", ""))
    if extension == "odt":
        return formats.export_odt_with_translated_text(content, text, source_meta.get("page_range", ""))
    if extension == "pptx":
        return formats.export_pptx_with_translated_text(content, text)
    if extension == "csv":
        columns = source_meta.get("columns", "")
        if not columns.strip():
            return formats.export_csv_auto_with_translated_text(content, text)
        return formats.export_csv_with_translated_text(content, columns, text)
    if extension == "xlsx":
        columns = source_meta.get("columns", "")
        if not columns.strip():
            return formats.export_xlsx_auto_with_translated_text(content, text)
        if formats.xlsx_is_multi_sheet_spec(columns):
            return formats.export_xlsx_multi_sheet_with_translated_text(content, columns, text)
        return formats.export_xlsx_with_translated_text(content, source_meta.get("sheet_name", ""), columns, text)
    if extension in ("html", "htm"):
        return formats.export_html_with_translated_text(content, text)
    if extension in ("srt", "vtt"):
        return formats.export_subtitle_with_translated_text(content, text)
    if extension == "json":
        return formats.export_json_with_translated_text(content, text)
    if extension in ("yaml", "yml"):
        return formats.export_yaml_with_translated_text(content, text)
    if extension == "po":
        return formats.export_po_with_translated_text(content, text)
    if extension in ("xlf", "xliff"):
        return formats.export_xliff_with_translated_text(content, text)
    if extension == "pdf":
        if source_meta.get("scan") == "true":
            return pdf_scan.export_scan_with_translated_text(content, text, source_meta.get("page_range", ""),
                                                    source_meta.get("scan_source", langs.AUTO_SOURCE),
                                                    source_meta.get("scan_language"),
                                                    source_meta.get("layout_model") == "true",
                                                    int(source_meta.get("scan_grouping", "0")))
        if source_meta.get("layout") == "true":
            return pdf_layout.export_pdf_layout_with_translated_text(content, text, source_meta.get("page_range", ""),
                                                                    source_meta.get("layout_model") == "true")
        return textdocs.create_text_pdf(text, content)
    if extension in ("md", "txt"):
        return text.encode("utf-8")
    raise HTTPException(status_code=400, detail="Unsupported history original format")


def history_original_export(item_id: str, extension: str, content: bytes, text: str,
                            source_meta: Dict[str, str]) -> bytes:
    """The export in its original format, built once and cached, see history_export_path.

    Written to a per-call temp file and moved into place with os.replace, which POSIX and
    Windows both guarantee is atomic - two concurrent downloads racing to fill the same cache
    then each write a whole, valid file, never one interleaved with the other's bytes.
    """
    cache_path = history_export_path(item_id, extension)
    if cache_path.exists():
        return cache_path.read_bytes()
    exported = export_original_history_content(extension, content, text, source_meta)
    temp_path = cache_path.with_name(f"{cache_path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_bytes(exported)
    os.replace(temp_path, cache_path)
    return exported
