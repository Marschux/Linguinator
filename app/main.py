# ... existing imports and setup code ...
import base64
import gc
import math
import os
import re
import secrets
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
from html.parser import HTMLParser
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from xml.etree import ElementTree

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter


def env_value(name: str, default: str, legacy_name: str = "") -> str:
    if name in os.environ:
        return os.environ[name]
    if legacy_name and legacy_name in os.environ:
        return os.environ[legacy_name]
    return default


MODEL_ID = env_value("LINGUINATOR_MODEL", "facebook/nllb-200-distilled-600M", "NLLB_MODEL")
DEVICE_SETTING = env_value("LINGUINATOR_DEVICE", "cpu", "NLLB_DEVICE")
MAX_CHARS = int(env_value("LINGUINATOR_MAX_CHARS", "6000", "NLLB_MAX_CHARS"))
MAX_FILE_MB = int(env_value("LINGUINATOR_MAX_FILE_MB", "50", "NLLB_MAX_FILE_MB"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = MAX_FILE_BYTES * 10
HISTORY_DAYS = int(env_value("LINGUINATOR_HISTORY_DAYS", "7", "NLLB_HISTORY_DAYS"))
HISTORY_DIR = Path(env_value("LINGUINATOR_HISTORY_DIR", "/data/history", "NLLB_HISTORY_DIR"))
JOB_WORKERS = max(1, int(env_value("LINGUINATOR_JOB_WORKERS", "1", "NLLB_JOB_WORKERS")))
JOBS_DIR = Path(env_value("LINGUINATOR_JOBS_DIR", str(HISTORY_DIR / "jobs"), "NLLB_JOBS_DIR"))
CPU_THREADS = int(env_value("LINGUINATOR_CPU_THREADS", "0", "NLLB_CPU_THREADS"))
CPU_INTEROP_THREADS = int(env_value("LINGUINATOR_CPU_INTEROP_THREADS", "0", "NLLB_CPU_INTEROP_THREADS"))
DEFAULT_SOURCE = env_value("LINGUINATOR_DEFAULT_SOURCE", "eng_Latn", "NLLB_DEFAULT_SOURCE")
DEFAULT_TARGET = env_value("LINGUINATOR_DEFAULT_TARGET", "deu_Latn", "NLLB_DEFAULT_TARGET")
OCR_ENABLED = env_value("LINGUINATOR_ENABLE_OCR", "true", "NLLB_ENABLE_OCR").lower() in ("1", "true", "yes", "on")
OCR_LANGUAGE = env_value("LINGUINATOR_OCR_LANGUAGE", "deu+eng", "NLLB_OCR_LANGUAGE")
AUTH_ENABLED = env_value("LINGUINATOR_AUTH_ENABLED", "false", "NLLB_AUTH_ENABLED").lower() in ("1", "true", "yes", "on")
AUTH_USERNAME = env_value("LINGUINATOR_AUTH_USERNAME", "admin", "NLLB_AUTH_USERNAME")
AUTH_PASSWORD = env_value("LINGUINATOR_AUTH_PASSWORD", "", "NLLB_AUTH_PASSWORD")
MODEL_IDLE_UNLOAD_ENABLED = env_value("LINGUINATOR_UNLOAD_MODEL_AFTER_IDLE", "true").lower() in ("1", "true", "yes", "on")
MODEL_IDLE_SECONDS = int(env_value("LINGUINATOR_MODEL_IDLE_SECONDS", "1200"))
PUBLIC_URL = os.getenv("LINGUINATOR_PUBLIC_URL", "").rstrip("/")
TRUST_PROXY_HEADERS = os.getenv("LINGUINATOR_TRUST_PROXY_HEADERS", "true").lower() in ("1", "true", "yes", "on")
PDF_LOW_TEXT_CHARS = 20
PDF_PAGE_WIDTH = 595
PDF_PAGE_HEIGHT = 842
PDF_MARGIN = 54
PDF_LINE_HEIGHT = 14
PDF_FONT_SIZE = 11
PDF_HEADING_FONT_SIZE = 15
PDF_FOOTER_FONT_SIZE = 9
# Average glyph width as a fraction of the font size, used when no real font metrics are
# available. Calibration knob: too small leaves old text peeking out from under the overlay,
# too large covers neighbouring content.
PDF_AVG_CHAR_WIDTH = 0.5
# How far the overlay may shrink the font to make a longer translation fit its original lines.
PDF_LAYOUT_MIN_SCALE = 0.7
# Safety margin on the estimated width of the original text when covering it. Anything left
# uncovered shows through next to the translation; overshoot is clamped at neighbouring columns.
PDF_COVER_WIDTH_FACTOR = 1.05
PDF_FONT_FILE = env_value("LINGUINATOR_PDF_FONT", "")
PDF_FONT_BOLD_FILE = env_value("LINGUINATOR_PDF_FONT_BOLD", "")
PDF_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
PDF_FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
NLLB_LANGUAGE_CODES = (
    "ace_Arab", "ace_Latn", "acm_Arab", "acq_Arab", "aeb_Arab", "afr_Latn",
    "ajp_Arab", "aka_Latn", "amh_Ethi", "apc_Arab", "arb_Arab", "ars_Arab",
    "ary_Arab", "arz_Arab", "asm_Beng", "ast_Latn", "awa_Deva", "ayr_Latn",
    "azb_Arab", "azj_Latn", "bak_Cyrl", "bam_Latn", "ban_Latn", "bel_Cyrl",
    "bem_Latn", "ben_Beng", "bho_Deva", "bjn_Arab", "bjn_Latn", "bod_Tibt",
    "bos_Latn", "bug_Latn", "bul_Cyrl", "cat_Latn", "ceb_Latn", "ces_Latn",
    "cjk_Latn", "ckb_Arab", "crh_Latn", "cym_Latn", "dan_Latn", "deu_Latn",
    "dik_Latn", "dyu_Latn", "dzo_Tibt", "ell_Grek", "eng_Latn", "epo_Latn",
    "est_Latn", "eus_Latn", "ewe_Latn", "fao_Latn", "fij_Latn", "fin_Latn",
    "fon_Latn", "fra_Latn", "fur_Latn", "fuv_Latn", "gla_Latn", "gle_Latn",
    "glg_Latn", "grn_Latn", "guj_Gujr", "hat_Latn", "hau_Latn", "heb_Hebr",
    "hin_Deva", "hne_Deva", "hrv_Latn", "hun_Latn", "hye_Armn", "ibo_Latn",
    "ilo_Latn", "ind_Latn", "isl_Latn", "ita_Latn", "jav_Latn", "jpn_Jpan",
    "kab_Latn", "kac_Latn", "kam_Latn", "kan_Knda", "kas_Arab", "kas_Deva",
    "kat_Geor", "knc_Arab", "knc_Latn", "kaz_Cyrl", "kbp_Latn", "kea_Latn",
    "khm_Khmr", "kik_Latn", "kin_Latn", "kir_Cyrl", "kmb_Latn", "kon_Latn",
    "kor_Hang", "kmr_Latn", "lao_Laoo", "lvs_Latn", "lij_Latn", "lim_Latn",
    "lin_Latn", "lit_Latn", "lmo_Latn", "ltg_Latn", "ltz_Latn", "lua_Latn",
    "lug_Latn", "luo_Latn", "lus_Latn", "mag_Deva", "mai_Deva", "mal_Mlym",
    "mar_Deva", "min_Arab", "min_Latn", "mkd_Cyrl", "plt_Latn", "mlt_Latn",
    "mni_Beng", "khk_Cyrl", "mos_Latn", "mri_Latn", "zsm_Latn", "mya_Mymr",
    "nld_Latn", "nno_Latn", "nob_Latn", "npi_Deva", "nso_Latn", "nus_Latn",
    "nya_Latn", "oci_Latn", "gaz_Latn", "ory_Orya", "pag_Latn", "pan_Guru",
    "pap_Latn", "pes_Arab", "pol_Latn", "por_Latn", "prs_Arab", "pbt_Arab",
    "quy_Latn", "ron_Latn", "run_Latn", "rus_Cyrl", "sag_Latn", "san_Deva",
    "sat_Olck", "scn_Latn", "shn_Mymr", "sin_Sinh", "slk_Latn", "slv_Latn",
    "smo_Latn", "sna_Latn", "snd_Arab", "som_Latn", "sot_Latn", "spa_Latn",
    "als_Latn", "srd_Latn", "srp_Cyrl", "ssw_Latn", "sun_Latn", "swe_Latn",
    "swh_Latn", "szl_Latn", "tam_Taml", "taq_Latn", "taq_Tfng", "tel_Telu",
    "tgk_Cyrl", "tgl_Latn", "tha_Thai", "tir_Ethi", "tpi_Latn", "tsn_Latn",
    "tso_Latn", "tuk_Latn", "tum_Latn", "tur_Latn", "twi_Latn", "tzm_Tfng",
    "uig_Arab", "ukr_Cyrl", "umb_Latn", "urd_Arab", "uzn_Latn", "vec_Latn",
    "vie_Latn", "war_Latn", "wol_Latn", "xho_Latn", "ydd_Hebr", "yor_Latn",
    "yue_Hant", "zho_Hans", "zho_Hant", "zul_Latn",
)

if CPU_THREADS > 0:
    os.environ.setdefault("OMP_NUM_THREADS", str(CPU_THREADS))
    os.environ.setdefault("MKL_NUM_THREADS", str(CPU_THREADS))

ElementTree.register_namespace("w", "http://schemas.openxmlformats.org/wordprocessingml/2006/main")
ElementTree.register_namespace("office", "urn:oasis:names:tc:opendocument:xmlns:office:1.0")
ElementTree.register_namespace("text", "urn:oasis:names:tc:opendocument:xmlns:text:1.0")
ElementTree.register_namespace("s", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
ElementTree.register_namespace("a", "http://schemas.openxmlformats.org/drawingml/2006/main")
ElementTree.register_namespace("xlf", "urn:oasis:names:tc:xliff:document:1.2")

APP_DIR = Path(__file__).resolve().parent


def normalized_root_path(value: str) -> str:
    path = value.strip().strip("/")
    return f"/{path}" if path else ""


ROOT_PATH = normalized_root_path(os.getenv("LINGUINATOR_ROOT_PATH", ""))

app = FastAPI(title="Linguinator", version="0.1.0", root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
JOBS: Dict[str, Dict[str, Any]] = {}
JOB_RUNNERS: Dict[str, Tuple[Callable[..., None], Tuple[Any, ...]]] = {}
JOBS_LOCK = threading.RLock()
JOBS_CONDITION = threading.Condition(JOBS_LOCK)
QUEUE_WORKERS_STARTED = False
MODEL_LOCK = threading.RLock()
MODEL_ACTIVE_USERS = 0
MODEL_LAST_USED = 0.0


class TranslateRequest(BaseModel):
    q: Union[str, List[str]]
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET


class PdfExportRequest(BaseModel):
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
    return secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(password, AUTH_PASSWORD)


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if not AUTH_ENABLED or request.url.path == "/health":
        return await call_next(request)
    if not AUTH_PASSWORD:
        return Response("Authentication is enabled but LINGUINATOR_AUTH_PASSWORD is not set", status_code=500)
    if not basic_auth_valid(request.headers.get("authorization", "")):
        return unauthorized_response()
    return await call_next(request)


def selected_device():
    if DEVICE_SETTING != "cuda":
        return "cpu"
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def configure_torch_threads(torch_module):
    if CPU_THREADS > 0 and hasattr(torch_module, "set_num_threads"):
        torch_module.set_num_threads(CPU_THREADS)
    if CPU_INTEROP_THREADS > 0 and hasattr(torch_module, "set_num_interop_threads"):
        try:
            torch_module.set_num_interop_threads(CPU_INTEROP_THREADS)
        except RuntimeError:
            pass


@lru_cache(maxsize=1)
def load_tokenizer():
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("transformers is required for translation") from exc
    return AutoTokenizer.from_pretrained(MODEL_ID)


@lru_cache(maxsize=1)
def load_model():
    try:
        import torch
        from transformers import AutoModelForSeq2SeqLM
    except ImportError as exc:
        raise RuntimeError("torch and transformers are required for translation") from exc
    configure_torch_threads(torch)
    device = selected_device()
    tokenizer = load_tokenizer()
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_ID)
    model.to(device)
    model.eval()
    return tokenizer, model, device, torch


def model_cache_loaded() -> bool:
    return load_model.cache_info().currsize > 0 or load_tokenizer.cache_info().currsize > 0


def begin_model_use():
    global MODEL_ACTIVE_USERS
    with MODEL_LOCK:
        MODEL_ACTIVE_USERS += 1


def end_model_use():
    global MODEL_ACTIVE_USERS, MODEL_LAST_USED
    with MODEL_LOCK:
        MODEL_ACTIVE_USERS = max(0, MODEL_ACTIVE_USERS - 1)
        MODEL_LAST_USED = time.time()


def unload_model_cache() -> bool:
    torch_module = None
    with MODEL_LOCK:
        if MODEL_ACTIVE_USERS > 0:
            return False
        if not model_cache_loaded():
            return False
        if load_model.cache_info().currsize > 0:
            try:
                _, _, _, torch_module = load_model()
            except Exception:
                torch_module = None
        load_model.cache_clear()
        load_tokenizer.cache_clear()
    gc.collect()
    cuda = getattr(torch_module, "cuda", None) if torch_module else None
    if cuda and hasattr(cuda, "is_available") and hasattr(cuda, "empty_cache") and cuda.is_available():
        cuda.empty_cache()
    return True


def unload_model_if_idle(now: float | None = None) -> bool:
    if not MODEL_IDLE_UNLOAD_ENABLED or MODEL_IDLE_SECONDS <= 0:
        return False
    current_time = time.time() if now is None else now
    with MODEL_LOCK:
        if MODEL_ACTIVE_USERS > 0 or MODEL_LAST_USED <= 0:
            return False
        if current_time - MODEL_LAST_USED < MODEL_IDLE_SECONDS:
            return False
    return unload_model_cache()


def model_idle_unloader():
    while True:
        sleep_seconds = min(60, max(1, MODEL_IDLE_SECONDS // 4 or 1))
        time.sleep(sleep_seconds)
        unload_model_if_idle()


if MODEL_IDLE_UNLOAD_ENABLED and MODEL_IDLE_SECONDS > 0:
    threading.Thread(target=model_idle_unloader, daemon=True).start()


def language_codes():
    if "nllb" in MODEL_ID.lower():
        return sorted(NLLB_LANGUAGE_CODES)
    try:
        tokenizer = load_tokenizer()
    except RuntimeError:
        return sorted(NLLB_LANGUAGE_CODES)
    codes = getattr(tokenizer, "additional_special_tokens", [])
    return sorted(
        code for code in codes
        if "_" in code and len(code.split("_", 1)[0]) == 3
    ) or sorted(NLLB_LANGUAGE_CODES)


def translate_one(text: str, source: str, target: str) -> str:
    if not text:
        return ""

    begin_model_use()
    try:
        tokenizer, model, device, torch_module = load_model()
        tokenizer.src_lang = source
        inputs = tokenizer(text, return_tensors="pt", truncation=True).to(device)
        forced_bos_token_id = tokenizer.convert_tokens_to_ids(target)

        with torch_module.inference_mode():
            generated = model.generate(
                **inputs,
                forced_bos_token_id=forced_bos_token_id,
                max_new_tokens=1024,
                num_beams=4,
            )
        return tokenizer.batch_decode(generated, skip_special_tokens=True)[0]
    finally:
        end_model_use()


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


class EmbeddedFont:
    """A TrueType font loaded from disk, ready to be embedded as a PDF CID font."""

    def __init__(self, name: str, data: bytes, glyph_ids: Dict[int, int], widths: Dict[int, int], metrics: Dict[str, int]):
        self.name = name
        self.data = data
        self.glyph_ids = glyph_ids
        self.widths = widths
        self.metrics = metrics

    def glyph_id(self, codepoint: int) -> int:
        return self.glyph_ids.get(codepoint, self.glyph_ids.get(ord("?"), 0))

    def encode(self, text: str) -> str:
        return "".join(f"{self.glyph_id(ord(char)):04X}" for char in text)

    def text_width(self, text: str, size: float) -> float:
        total = sum(self.widths.get(ord(char), 500) for char in text)
        return total * size / 1000.0


@lru_cache(maxsize=2)
def load_embedded_font(bold: bool = False) -> Optional[EmbeddedFont]:
    """Load a TrueType font for PDF embedding, or None to fall back to base-14 Helvetica.

    Without an embedded font a PDF can only show WinAnsi characters, so any non-Latin target
    language (Cyrillic, Greek, ...) would come out as garbage.
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return None

    configured = PDF_FONT_BOLD_FILE if bold else PDF_FONT_FILE
    candidates = PDF_FONT_BOLD_CANDIDATES if bold else PDF_FONT_CANDIDATES
    for path in (configured, *candidates):
        if not path or not Path(path).exists():
            continue
        try:
            ttf = TTFont(path, fontNumber=0, lazy=True)
            units = ttf["head"].unitsPerEm or 1000
            scale = 1000.0 / units
            metrics = ttf["hmtx"].metrics
            glyph_ids: Dict[int, int] = {}
            widths: Dict[int, int] = {}
            for codepoint, glyph_name in ttf.getBestCmap().items():
                glyph_ids[codepoint] = ttf.getGlyphID(glyph_name)
                widths[codepoint] = round(metrics[glyph_name][0] * scale)
            head = ttf["head"]
            os2 = ttf["OS/2"] if "OS/2" in ttf else None
            return EmbeddedFont(
                re.sub(r"[^A-Za-z0-9-]", "", Path(path).stem) or "EmbeddedFont",
                Path(path).read_bytes(),
                glyph_ids,
                widths,
                {
                    "x_min": round(head.xMin * scale),
                    "y_min": round(head.yMin * scale),
                    "x_max": round(head.xMax * scale),
                    "y_max": round(head.yMax * scale),
                    "ascent": round(ttf["hhea"].ascent * scale),
                    "descent": round(ttf["hhea"].descent * scale),
                    "cap_height": round(getattr(os2, "sCapHeight", 0) * scale) or 700,
                },
            )
        except Exception:
            continue
    return None


def pdf_measure_text(text: str, size: float, bold: bool = False) -> float:
    font = load_embedded_font(bold)
    if font:
        return font.text_width(text, size)
    return len(text) * PDF_AVG_CHAR_WIDTH * size


def pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_text_object(text: str, bold: bool = False) -> str:
    font = load_embedded_font(bold)
    if font:
        return "<" + font.encode(text) + ">"
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return "<" + (bytes.fromhex("FEFF") + text.encode("utf-16-be")).hex().upper() + ">"
    return f"({pdf_escape(text)})"


def wrap_pdf_line(text: str, max_chars: int = 88) -> List[str]:
    if not text:
        return [""]
    lines = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        lines.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    lines.append(remaining)
    return lines


def pdf_line_command(text: str, x: float, y: float, font: str = "F1", size: float = PDF_FONT_SIZE) -> str:
    return f"BT /{font} {size:g} Tf {x:.2f} {y:.2f} Td {pdf_text_object(text, font == 'F2')} Tj ET"


def pdf_cover_command(x: float, y: float, width: float, height: float) -> str:
    # ponytail: covers with plain white. Text sitting on a coloured background gets a white
    # patch; reading the actual background colour out of the content stream would fix it.
    return f"1 1 1 rg {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f 0 g"


def markdown_page_sections(text: str) -> List[Dict[str, str]]:
    sections = pdf_sections(text)
    if not sections:
        return [{"page_number": "", "text": text.strip()}]
    return [{"page_number": page_number, "text": page_text} for page_number, page_text in sections]


def pdf_render_lines(text: str) -> List[Dict[str, Any]]:
    lines = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for paragraph in normalized.split("\n"):
        stripped = paragraph.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                for line in wrap_pdf_line(heading, 68):
                    lines.append({"text": line, "font": "F2", "size": PDF_HEADING_FONT_SIZE, "line_height": 18})
                lines.append({"text": "", "font": "F1", "size": PDF_FONT_SIZE, "line_height": 8})
                continue
        wrapped = wrap_pdf_line(stripped)
        for line in wrapped:
            lines.append({"text": line, "font": "F1", "size": PDF_FONT_SIZE, "line_height": PDF_LINE_HEIGHT})
    return lines


def paginate_pdf_lines(lines: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    pages = [[]]
    usable_height = PDF_PAGE_HEIGHT - (2 * PDF_MARGIN) - 26
    used_height = 0
    for line in lines:
        line_height = line["line_height"]
        if pages[-1] and used_height + line_height > usable_height:
            pages.append([])
            used_height = 0
        pages[-1].append(line)
        used_height += line_height
    return pages or [[]]


def pdf_document_pages(text: str) -> List[Dict[str, Any]]:
    document_pages = []
    for section in markdown_page_sections(text):
        content_pages = paginate_pdf_lines(pdf_render_lines(section["text"]))
        for index, lines in enumerate(content_pages, start=1):
            document_pages.append({
                "source_page": section["page_number"],
                "continuation": index > 1,
                "lines": lines,
            })
    return document_pages or [{"source_page": "", "continuation": False, "lines": []}]


def pdf_stream_object(dictionary: str, stream: bytes) -> bytes:
    return f"<< {dictionary} /Length {len(stream)} >>\nstream\n".encode("utf-8") + stream + b"\nendstream"


def pdf_to_unicode_stream(font: EmbeddedFont, codepoints: List[int]) -> bytes:
    """A ToUnicode CMap, so text in the generated PDF stays selectable and searchable
    even though the content stream addresses glyphs by id."""
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    pairs = sorted({(font.glyph_id(codepoint), codepoint) for codepoint in codepoints})
    for start in range(0, len(pairs), 100):
        block = pairs[start:start + 100]
        lines.append(f"{len(block)} beginbfchar")
        for glyph_id, codepoint in block:
            target = chr(codepoint).encode("utf-16-be").hex().upper()
            lines.append(f"<{glyph_id:04X}> <{target}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("utf-8")


def pdf_font_file(font: EmbeddedFont, codepoints: List[int]) -> bytes:
    """Strip the outlines of glyphs the document never draws. `retain_gids` keeps the original
    glyph ids valid, so the cached cmap/width tables stay usable."""
    try:
        from fontTools import subset
        from fontTools.ttLib import TTFont

        ttf = TTFont(BytesIO(font.data), fontNumber=0)
        subsetter = subset.Subsetter(options=subset.Options(retain_gids=True, notdef_outline=True))
        subsetter.populate(unicodes=codepoints)
        subsetter.subset(ttf)
        output = BytesIO()
        ttf.save(output)
        return output.getvalue()
    except Exception:
        return font.data


def pdf_font_objects(font: EmbeddedFont, codepoints: List[int], first_id: int) -> List[bytes]:
    """Five objects describing one embedded font: file, descriptor, ToUnicode, CID font, Type0."""
    file_id, descriptor_id, to_unicode_id, cid_id = first_id, first_id + 1, first_id + 2, first_id + 3
    widths = sorted({(font.glyph_id(codepoint), font.widths.get(codepoint, 500)) for codepoint in codepoints})
    width_array = " ".join(f"{glyph_id} [{width}]" for glyph_id, width in widths)
    metrics = font.metrics
    font_data = pdf_font_file(font, codepoints)
    return [
        pdf_stream_object(f"/Length1 {len(font_data)}", font_data),
        (
            f"<< /Type /FontDescriptor /FontName /{font.name} /Flags 4 "
            f"/FontBBox [{metrics['x_min']} {metrics['y_min']} {metrics['x_max']} {metrics['y_max']}] "
            f"/ItalicAngle 0 /Ascent {metrics['ascent']} /Descent {metrics['descent']} "
            f"/CapHeight {metrics['cap_height']} /StemV 80 /FontFile2 {file_id} 0 R >>"
        ).encode("utf-8"),
        pdf_stream_object("", pdf_to_unicode_stream(font, codepoints)),
        (
            f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{font.name} "
            f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            f"/FontDescriptor {descriptor_id} 0 R /DW 1000 /W [{width_array}] /CIDToGIDMap /Identity >>"
        ).encode("utf-8"),
        (
            f"<< /Type /Font /Subtype /Type0 /BaseFont /{font.name} /Encoding /Identity-H "
            f"/DescendantFonts [{cid_id} 0 R] /ToUnicode {to_unicode_id} 0 R >>"
        ).encode("utf-8"),
    ]


def pdf_document_font_texts(pages: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Every string the document draws, grouped by font resource name, so each font is only
    embedded if it is actually used and only needs widths for the characters it draws."""
    texts: Dict[str, List[str]] = {"F1": [], "F2": []}
    for page in pages:
        for line in page["lines"]:
            texts[line["font"]].append(line["text"])
        if page["source_page"]:
            texts["F2"].append("Page " + page["source_page"] + " continued")
        if page.get("footer", True):
            texts["F1"].append("0123456789")
    return texts


def create_pdf_from_pages(pages: List[Dict[str, Any]]) -> bytes:
    objects: List[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    page_refs = []
    next_object_id = 3

    # Embedded fonts are shared by every page, so they are allocated before the pages.
    font_texts = pdf_document_font_texts(pages)
    font_resources = []
    for index, bold in enumerate((False, True)):
        name = f"F{index + 1}"
        font = load_embedded_font(bold) if any(font_texts[name]) else None
        if not font:
            base = "Helvetica-Bold" if bold else "Helvetica"
            font_resources.append(f"/{name} << /Type /Font /Subtype /Type1 /BaseFont /{base} >>")
            continue
        codepoints = sorted({ord(char) for text in font_texts[name] for char in text})
        objects.extend(pdf_font_objects(font, codepoints, next_object_id))
        font_resources.append(f"/{name} {next_object_id + 4} 0 R")
        next_object_id += 5

    resources = "/Font << " + " ".join(font_resources) + " >>"

    for output_page_number, page in enumerate(pages, start=1):
        width = page.get("width", PDF_PAGE_WIDTH)
        height = page.get("height", PDF_PAGE_HEIGHT)
        margin = page.get("margin", PDF_MARGIN)
        page_id = next_object_id
        content_id = next_object_id + 1
        next_object_id += 2
        page_refs.append(f"{page_id} 0 R")
        commands = list(page.get("commands", []))
        y = height - margin
        if page["source_page"]:
            heading = "Page " + page["source_page"]
            if page["continuation"]:
                heading += " continued"
            commands.append(pdf_line_command(heading, margin, y, "F2", PDF_HEADING_FONT_SIZE))
            y -= 24
        for line in page["lines"]:
            if line["text"]:
                # Lines carrying their own coordinates are placed absolutely (layout overlay),
                # everything else flows down the page from the margin.
                commands.append(
                    pdf_line_command(line["text"], line.get("x", margin), line.get("y", y), line["font"], line["size"])
                )
            y -= line["line_height"]
        if page.get("footer", True):
            commands.append(
                pdf_line_command(
                    f"{output_page_number}",
                    width - margin,
                    margin // 2,
                    "F1",
                    PDF_FOOTER_FONT_SIZE,
                )
            )
        stream = "\n".join(commands).encode("utf-8")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:.2f} {height:.2f}] "
                f"/Resources << {resources} >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("utf-8")
        )
        objects.append(pdf_stream_object("", stream))

    objects.insert(1, f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>".encode("utf-8"))
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode("utf-8") + obj + b"\nendobj\n")
    xref_offset = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(data)


def create_text_pdf(text: str) -> bytes:
    return create_pdf_from_pages(pdf_document_pages(text))


def cleanup_history():
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - (HISTORY_DAYS * 86400)
    for path in HISTORY_DIR.glob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def history_safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")
    return safe[:80] or "translation"


def file_extension(name: str) -> str:
    extension = Path(name or "").suffix.lower().lstrip(".")
    return re.sub(r"[^a-z0-9]+", "", extension)[:12]


def history_source_path(item_id: str, extension: str) -> Path:
    safe_extension = file_extension("x." + extension)
    return HISTORY_DIR / f"{item_id}.source.{safe_extension}"


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
    md_path = HISTORY_DIR / f"{base}.md"
    json_path = HISTORY_DIR / f"{base}.json"
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
    for meta_path in sorted(HISTORY_DIR.glob("*.json"), reverse=True):
        try:
            item = json.loads(meta_path.read_text(encoding="utf-8"))
            md_path = HISTORY_DIR / f"{item['id']}.md"
            source_ext = item.get("source_extension", "")
            source_file = history_source_path(item["id"], source_ext) if source_ext else None
            item["size_bytes"] = md_path.stat().st_size if md_path.exists() else 0
            item["has_source_file"] = bool(source_file and source_file.exists())
            items.append(item)
        except Exception:
            continue
    return items


def history_paths(item_id: str):
    if ".." in item_id or "/" in item_id or "\\" in item_id:
        raise HTTPException(status_code=400, detail="Invalid history id")
    return HISTORY_DIR / f"{item_id}.md", HISTORY_DIR / f"{item_id}.json"


def history_item(item_id: str) -> Dict[str, Any]:
    _, json_path = history_paths(item_id)
    if not json_path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    return json.loads(json_path.read_text(encoding="utf-8"))


async def read_upload_bytes(file: UploadFile, label: str = "File") -> bytes:
    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} exceeds {MAX_FILE_MB} MB")
    return content


def job_json_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def job_payload_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.payload"


def persisted_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in job.items() if key != "result"}


def public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    visible = dict(job)
    visible.pop("text", None)
    visible.pop("payload_path", None)
    visible.pop("source_payload_path", None)
    return visible


def job_with_recovered_result(job: Dict[str, Any]) -> Dict[str, Any]:
    visible = public_job(job)
    if visible.get("status") == "complete" and not visible.get("result") and visible.get("history_id"):
        history_path, _ = history_paths(visible["history_id"])
        if history_path.exists():
            visible["result"] = history_path.read_text(encoding="utf-8")
    return visible


def persist_job(job: Dict[str, Any]):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_json_path(job["id"]).write_text(json.dumps(persisted_job(job)), encoding="utf-8")


def persist_all_jobs():
    with JOBS_LOCK:
        for job in JOBS.values():
            persist_job(job)


def load_persisted_jobs():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with JOBS_LOCK:
        for meta_path in JOBS_DIR.glob("*.json"):
            try:
                job = json.loads(meta_path.read_text(encoding="utf-8"))
                if job.get("status") == "running":
                    job["status"] = "queued"
                    job["message"] = "Requeued after restart"
                    job["started_at"] = None
                job.setdefault("result", None)
                job.setdefault("error", None)
                JOBS[job["id"]] = job
            except Exception:
                continue


def job_sort_key(job: Dict[str, Any]):
    return job.get("queued_at") or job.get("started_at") or job.get("finished_at") or 0


def queue_positions_by_job_id() -> Dict[str, int]:
    queued_position = 0
    positions: Dict[str, int] = {}
    for job in sorted(JOBS.values(), key=job_sort_key):
        if job.get("status") == "queued":
            queued_position += 1
            positions[job["id"]] = queued_position
    return positions


def list_jobs():
    with JOBS_LOCK:
        positions = queue_positions_by_job_id()
        jobs = [public_job(job) for job in sorted(JOBS.values(), key=job_sort_key)]
        for job in jobs:
            job["position"] = positions.get(job["id"])
        return jobs


def create_job(kind: str, source: str = "", target: str = "", label: str = "") -> str:
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "message": "Queued",
            "source": source,
            "target": target,
            "label": label,
            "queued_at": time.time(),
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
        persist_job(JOBS[job_id])
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
        persist_job(job)
        JOBS_CONDITION.notify_all()


def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        visible = job_with_recovered_result(job)
        visible["position"] = queue_positions_by_job_id().get(job_id)
        return visible


def control_job(job_id: str, action: str):
    if action == "pause":
        update_job(job_id, pause_requested=True, message="Pause requested")
    elif action == "resume":
        update_job(job_id, pause_requested=False, message="Resuming")
    elif action == "cancel":
        current = get_job(job_id)
        if current.get("status") == "queued":
            update_job(
                job_id,
                status="cancelled",
                cancel_requested=True,
                pause_requested=False,
                message="Cancelled",
                finished_at=time.time(),
            )
            cleanup_job_payload(job_id)
        else:
            update_job(job_id, cancel_requested=True, pause_requested=False, message="Stop requested")
    else:
        raise HTTPException(status_code=400, detail="Unknown job action")
    return get_job(job_id)


def register_job_runner(job_id: str, runner: Callable[..., None], args: Tuple[Any, ...]):
    with JOBS_LOCK:
        JOB_RUNNERS[job_id] = (runner, args)
        persist_job(JOBS[job_id])
        JOBS_CONDITION.notify_all()


def queued_job_without_active_worker(active_jobs: set) -> Optional[Dict[str, Any]]:
    for job in sorted(JOBS.values(), key=job_sort_key):
        if job.get("status") == "queued" and not job.get("cancel_requested") and job["id"] not in active_jobs:
            return job
    return None


def rebuild_runner_for_job(job: Dict[str, Any]) -> Optional[Tuple[Callable[..., None], Tuple[Any, ...]]]:
    job_id = job["id"]
    if job.get("kind") == "translate":
        source_payload_path = job.get("source_payload_path")
        source_content = b""
        if source_payload_path and Path(source_payload_path).exists():
            source_content = Path(source_payload_path).read_bytes()
        return run_text_job, (
            job_id,
            job.get("text", ""),
            job.get("source", DEFAULT_SOURCE),
            job.get("target", DEFAULT_TARGET),
            job.get("original_name", "text"),
            source_content,
            job.get("source_extension", ""),
            job.get("source_meta", {}),
        )
    if job.get("kind") == "translate-pdf":
        payload_path = job.get("payload_path")
        if not payload_path or not Path(payload_path).exists():
            update_job(job_id, status="failed", message="Failed", error="Queued PDF payload is missing", finished_at=time.time())
            return None
        return run_pdf_translate_job, (
            job_id,
            Path(payload_path).read_bytes(),
            job.get("content_type", "application/pdf"),
            job.get("source", DEFAULT_SOURCE),
            job.get("target", DEFAULT_TARGET),
            job.get("filename", "pdf"),
            job.get("page_range", ""),
            OCR_ENABLED,
        )
    if job.get("kind") == "translate-pdf-layout":
        payload_path = job.get("payload_path")
        if not payload_path or not Path(payload_path).exists():
            update_job(job_id, status="failed", message="Failed", error="Queued PDF payload is missing", finished_at=time.time())
            return None
        return run_pdf_layout_translate_job, (
            job_id,
            Path(payload_path).read_bytes(),
            job.get("source", DEFAULT_SOURCE),
            job.get("target", DEFAULT_TARGET),
            job.get("filename", "pdf"),
        )
    return None


def job_worker_loop():
    active_jobs = set()
    while True:
        with JOBS_CONDITION:
            while True:
                active_jobs = {
                    job_id for job_id, job in JOBS.items()
                    if job.get("status") == "running" and job_id in JOB_RUNNERS
                }
                if len(active_jobs) < JOB_WORKERS:
                    job = queued_job_without_active_worker(active_jobs)
                    if job:
                        break
                JOBS_CONDITION.wait()
            job_id = job["id"]
            runner_data = JOB_RUNNERS.get(job_id) or rebuild_runner_for_job(job)
            if not runner_data:
                continue
            JOB_RUNNERS[job_id] = runner_data
            job["status"] = "running"
            job["message"] = "Starting"
            if not job.get("started_at"):
                job["started_at"] = time.time()
            persist_job(job)
        runner, args = runner_data
        runner(*args)
        with JOBS_CONDITION:
            JOB_RUNNERS.pop(job["id"], None)
            JOBS_CONDITION.notify_all()


def ensure_queue_workers():
    global QUEUE_WORKERS_STARTED
    with JOBS_LOCK:
        if QUEUE_WORKERS_STARTED:
            return
        load_persisted_jobs()
        QUEUE_WORKERS_STARTED = True
        for _ in range(JOB_WORKERS):
            threading.Thread(target=job_worker_loop, daemon=True).start()
        JOBS_CONDITION.notify_all()


def wait_if_paused_or_cancelled(job_id: str):
    while True:
        job = get_job(job_id)
        if job.get("cancel_requested"):
            raise RuntimeError("Job stopped by user")
        if not job.get("pause_requested"):
            return
        update_job(job_id, status="paused", message="Paused")
        time.sleep(1)


def cleanup_job_payload(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
        payload_path = job.get("payload_path")
        if payload_path:
            Path(payload_path).unlink(missing_ok=True)
        source_payload_path = job.get("source_payload_path")
        if source_payload_path:
            Path(source_payload_path).unlink(missing_ok=True)
        if "text" in job:
            job["text"] = ""
        persist_job(job)


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
        raise HTTPException(
            status_code=500,
            detail=(
                "OCR tools are unavailable in this container: "
                + ", ".join(missing)
                + ". Rebuild or restart from a current image that includes the OCR binaries."
            ),
        )


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
    use_ocr: bool = False,
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
        if needs_ocr and use_ocr:
            ocr_text = ocr_pdf_page(content, index)
            if ocr_text:
                text = ocr_text

        if text:
            pages_with_text += 1
            if len(text) < PDF_LOW_TEXT_CHARS and not use_ocr:
                text += "\n\n> Warning: This page has very little extractable text and may need OCR."
            pages.append(f"# Page {index}\n\n{text}".strip())
        else:
            pages.append(f"# Page {index}\n\n> No extractable text found on this page. It may need OCR.")

    markdown = "\n\n".join(pages).strip()
    if not pages_with_text:
        ocr_detail = (
            f"OCR is configured for {OCR_LANGUAGE}, but no readable text was produced."
            if use_ocr
            else "OCR is disabled for this deployment. Set LINGUINATOR_ENABLE_OCR=true to use OCR fallback."
        )
        raise HTTPException(
            status_code=422,
            detail=f"No extractable text found. This PDF may be scanned, image-only, or protected. {ocr_detail}",
        )
    return markdown


def multiply_matrix(a: Tuple[float, ...], b: Tuple[float, ...]) -> Tuple[float, ...]:
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def pdf_font_widths(font_dict: Any) -> Tuple[Dict[Any, float], Dict[str, str]]:
    """Glyph widths of a font used in the source PDF, plus the mapping from the extracted
    text back to the keys those widths are stored under."""
    from pypdf._cmap import build_char_map_from_dict, build_font_width_map

    _, _, _, character_map = build_char_map_from_dict(200.0, font_dict)
    reverse_map: Dict[str, str] = {}
    for key, value in character_map.items():
        reverse_map.setdefault(value, key)
    return build_font_width_map(font_dict, 400.0), reverse_map


def pdf_run_width(text: str, size: float, font_dict: Any, cache: Dict[int, Any]) -> Tuple[float, bool]:
    """Width of an extracted run in the *original* font, and whether it could be measured.

    Falls back to an estimate from our own font metrics for fonts without usable width
    information. Only that estimate needs a safety margin when covering the original text.
    """
    from pypdf._cmap import compute_font_width

    key = id(font_dict)
    if key not in cache:
        try:
            cache[key] = pdf_font_widths(font_dict)
        except Exception:
            cache[key] = None
    entry = cache[key]
    if not entry:
        return pdf_measure_text(text, size), False
    width_map, reverse_map = entry
    try:
        total = sum(compute_font_width(width_map, reverse_map.get(char, char)) for char in text)
    except Exception:
        return pdf_measure_text(text, size), False
    return total * size / 1000.0, True


def pdf_page_runs(page) -> List[Dict[str, Any]]:
    """Every text run on the page with its position on the page and its rendered font size."""
    runs: List[Dict[str, Any]] = []
    width_cache: Dict[int, Any] = {}

    def visitor(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        matrix = multiply_matrix(tuple(tm), tuple(cm))
        scale = math.sqrt(abs(matrix[0] * matrix[3] - matrix[1] * matrix[2])) or 1.0
        size = abs(float(font_size or PDF_FONT_SIZE)) * scale or PDF_FONT_SIZE
        width, exact = pdf_run_width(text.strip(), size, font_dict, width_cache)
        runs.append({
            "text": text,
            "x": matrix[4],
            "y": matrix[5],
            "size": size,
            "width": width,
            "exact": exact,
        })

    page.extract_text(visitor_text=visitor)
    return runs


def group_pdf_lines(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge runs that share a baseline into lines.

    Grouping by baseline is the one grouping PDFs make reliable, which is why the layout
    pipeline builds on it instead of trying to detect blocks or columns geometrically.
    """
    baselines: List[List[Dict[str, Any]]] = []
    for run in sorted(runs, key=lambda item: -item["y"]):
        if baselines and abs(baselines[-1][0]["y"] - run["y"]) <= max(1.0, 0.3 * run["size"]):
            baselines[-1].append(run)
        else:
            baselines.append([run])

    lines: List[Dict[str, Any]] = []
    for baseline in baselines:
        line = None
        for run in sorted(baseline, key=lambda item: item["x"]):
            width = run.get("width") or pdf_measure_text(run["text"], run["size"])
            if line is None:
                line = {
                    "text": run["text"],
                    "x": run["x"],
                    "y": run["y"],
                    "right": run["x"] + width,
                    "size": run["size"],
                    "exact": run.get("exact", False),
                }
                continue
            gap = run["x"] - line["right"]
            separator = " " if gap > 0.2 * run["size"] and not line["text"].endswith(" ") else ""
            line["text"] += separator + run["text"]
            # Several runs drawn in one text block report the same position, so a run that does
            # not start beyond the current end continues from it.
            line["right"] = max(run["x"], line["right"]) + width
            line["size"] = max(line["size"], run["size"])
            line["exact"] = line["exact"] and run.get("exact", False)
        line["text"] = re.sub(r"\s+", " ", line["text"]).strip()
        if line["text"]:
            lines.append(line)
    return lines


def group_pdf_paragraphs(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bundle lines into paragraphs, purely so the model gets whole sentences.

    A wrong split only costs translation quality here, never placement: every line keeps its
    own coordinates and the translation is reflowed into exactly those.
    """
    paragraphs: List[Dict[str, Any]] = []
    for line in lines:
        current = paragraphs[-1] if paragraphs else None
        if current:
            previous = current["lines"][-1]
            spacing = previous["y"] - line["y"]
            fits = (
                0 < spacing <= 1.8 * max(previous["size"], line["size"])
                and abs(previous["x"] - line["x"]) <= 3
                and abs(previous["size"] - line["size"]) <= 0.2 * previous["size"]
            )
            if fits:
                current["lines"].append(line)
                continue
        paragraphs.append({"lines": [line]})
    for paragraph in paragraphs:
        paragraph["text"] = " ".join(line["text"] for line in paragraph["lines"])
    return paragraphs


def extract_pdf_layout(content: bytes, page_range: str = "") -> List[Dict[str, Any]]:
    try:
        reader = PdfReader(BytesIO(content))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}") from exc
    if not reader.pages:
        raise HTTPException(status_code=422, detail="PDF has no pages")

    pages = []
    for index in parse_page_range(page_range, len(reader.pages)):
        page = reader.pages[index - 1]
        box = page.mediabox
        # Rotated pages would need the whole overlay transformed; they keep their original text.
        rotated = int(page.get("/Rotate", 0) or 0) % 360 != 0
        lines = [] if rotated else group_pdf_lines(pdf_page_runs(page))
        pages.append({
            "number": index,
            "width": float(box.width),
            "height": float(box.height),
            "paragraphs": group_pdf_paragraphs(lines),
        })
    if not any(page["paragraphs"] for page in pages):
        raise HTTPException(
            status_code=422,
            detail="No positioned text found. This PDF may be scanned or image-only, "
                   "translate it with Plaintext enabled instead.",
        )
    return pages


def wrap_text_to_width(text: str, width: float, size: float) -> List[str]:
    lines: List[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and pdf_measure_text(candidate, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def cover_right_edge(line: Dict[str, Any], wanted: float, neighbours: List[Dict[str, Any]]) -> float:
    """Widen the cover as far as the estimate asks, but never into a neighbouring column."""
    for other in neighbours:
        if other is line:
            continue
        if abs(other["y"] - line["y"]) <= 0.6 * line["size"] and other["x"] > line["x"]:
            wanted = min(wanted, other["x"] - 1)
    return max(wanted, line["right"])


def reflow_paragraph(
    paragraph: Dict[str, Any],
    text: str,
    neighbours: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Cover the paragraph's original lines and lay the translation out in the same column.

    Returns the cover commands plus absolutely positioned text lines.
    """
    lines = paragraph["lines"]
    left = min(line["x"] for line in lines)
    width = max(max(line["right"] for line in lines) - left, 10.0)
    base_size = max(line["size"] for line in lines)
    if len(lines) > 1:
        leading = (lines[0]["y"] - lines[-1]["y"]) / (len(lines) - 1)
    else:
        leading = 1.2 * base_size

    size = base_size
    wrapped = wrap_text_to_width(text, width, size)
    while len(wrapped) > len(lines) and size > base_size * PDF_LAYOUT_MIN_SCALE:
        size = max(size * 0.95, base_size * PDF_LAYOUT_MIN_SCALE)
        wrapped = wrap_text_to_width(text, width, size)

    factor = 1.0 if all(line.get("exact") for line in lines) else PDF_COVER_WIDTH_FACTOR
    covers = []
    for line in lines:
        # Each line is covered to its own estimated end, so a short line does not paint over
        # whatever sits beside the paragraph.
        right = cover_right_edge(line, line["x"] + (line["right"] - line["x"]) * factor, neighbours or [])
        covers.append(
            pdf_cover_command(line["x"] - 1, line["y"] - 0.25 * line["size"], right - line["x"] + 2, 1.2 * line["size"])
        )
    placed = []
    for index, wrapped_line in enumerate(wrapped):
        # Translations longer than the original keep running below the last line: overflowing
        # is recoverable for the reader, silently cut off text is not.
        y = lines[index]["y"] if index < len(lines) else lines[-1]["y"] - leading * (index - len(lines) + 1)
        placed.append({"text": wrapped_line, "font": "F1", "size": size, "line_height": 0, "x": left, "y": y})
    return covers, placed


def render_pdf_layout_overlay(content: bytes, pages: List[Dict[str, Any]], translations: List[str]) -> bytes:
    """Stamp the translated text onto the original pages, so images, icons and vector graphics
    survive untouched."""
    overlay_pages = []
    index = 0
    for page in pages:
        commands: List[str] = []
        lines: List[Dict[str, Any]] = []
        page_lines = [line for paragraph in page["paragraphs"] for line in paragraph["lines"]]
        for paragraph in page["paragraphs"]:
            # A paragraph without a translation keeps its original text rather than being
            # covered with nothing.
            if index < len(translations) and translations[index].strip():
                covers, placed = reflow_paragraph(paragraph, translations[index], page_lines)
                commands.extend(covers)
                lines.extend(placed)
            index += 1
        overlay_pages.append({
            "width": page["width"],
            "height": page["height"],
            "margin": 0,
            "source_page": "",
            "continuation": False,
            "footer": False,
            "lines": lines,
            "commands": commands,
        })

    overlay_reader = PdfReader(BytesIO(create_pdf_from_pages(overlay_pages)))
    reader = PdfReader(BytesIO(content))
    writer = PdfWriter()
    selected = {page["number"] for page in pages}
    overlays = {page["number"]: overlay_reader.pages[position] for position, page in enumerate(pages)}
    for number in range(1, len(reader.pages) + 1):
        original = reader.pages[number - 1]
        if number in selected:
            original.merge_page(overlays[number])
        writer.add_page(original)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def export_pdf_layout_with_translated_text(content: bytes, translated_text: str) -> bytes:
    # Split without dropping empty blocks: translations are matched to paragraphs by position.
    blocks = [block.strip() for block in translated_text.split("\n\n")]
    return render_pdf_layout_overlay(content, extract_pdf_layout(content), blocks)


def exception_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def extract_docx_text_from_bytes(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as docx:
            ensure_zip_size(docx)
            parts = docx_text_part_names(docx)
            documents = {part: docx.read(part) for part in parts}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read DOCX: {exc}") from exc

    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

    paragraphs = []
    for part, document in documents.items():
        try:
            root = ElementTree.fromstring(document)
        except ElementTree.ParseError as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse DOCX XML {part}: {exc}") from exc
        for paragraph in root.findall(".//w:p", namespaces):
            parts = [node.text or "" for node in paragraph.findall(".//w:t", namespaces)]
            text = "".join(parts).strip()
            if text:
                paragraphs.append(text)

    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in DOCX")
    return result


def translated_blocks(text: str) -> List[str]:
    return [block.strip() for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]


def ensure_zip_size(workbook: zipfile.ZipFile):
    uncompressed_size = sum(item.file_size for item in workbook.infolist())
    if uncompressed_size > MAX_ZIP_UNCOMPRESSED_BYTES:
        raise HTTPException(status_code=413, detail=f"Archive expands beyond {MAX_ZIP_UNCOMPRESSED_BYTES // 1024 // 1024} MB")


def write_zip_with_replacement(content: bytes, replacements: Dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(content)) as source:
        ensure_zip_size(source)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                normalized_name = item.filename.replace("\\", "/")
                if normalized_name.startswith("/") or normalized_name.startswith("../") or "/../" in normalized_name:
                    raise HTTPException(status_code=400, detail="Archive contains unsafe paths")
                data = replacements.get(item.filename)
                if data is None:
                    data = source.read(item.filename)
                target.writestr(item, data)
    return output.getvalue()


def docx_text_part_names(docx: zipfile.ZipFile) -> List[str]:
    preferred = [
        "word/document.xml",
        "word/header",
        "word/footer",
        "word/footnotes.xml",
        "word/endnotes.xml",
        "word/comments.xml",
    ]
    names = [
        item.filename
        for item in docx.infolist()
        if item.filename.startswith("word/")
        and item.filename.endswith(".xml")
        and (
            item.filename == "word/document.xml"
            or re.match(r"word/header\d*\.xml$", item.filename)
            or re.match(r"word/footer\d*\.xml$", item.filename)
            or item.filename in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}
        )
    ]
    return sorted(
        names,
        key=lambda name: next(
            (index for index, prefix in enumerate(preferred) if name == prefix or name.startswith(prefix)),
            len(preferred),
        ),
    )


def export_docx_with_translated_text(content: bytes, translated_text: str) -> bytes:
    blocks = translated_blocks(translated_text)
    if not blocks:
        raise HTTPException(status_code=400, detail="No translated text to export")
    try:
        with zipfile.ZipFile(BytesIO(content)) as docx:
            ensure_zip_size(docx)
            documents = {part: docx.read(part) for part in docx_text_part_names(docx)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read DOCX: {exc}") from exc

    namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    replacements = {}
    block_index = 0
    for part, document in documents.items():
        root = ElementTree.fromstring(document)
        changed = False
        for paragraph in root.findall(".//w:p", namespaces):
            text_nodes = paragraph.findall(".//w:t", namespaces)
            if not "".join(node.text or "" for node in text_nodes).strip():
                continue
            if block_index >= len(blocks):
                break
            for node_index, node in enumerate(text_nodes):
                node.text = blocks[block_index] if node_index == 0 else ""
            block_index += 1
            changed = True
        if changed:
            replacements[part] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        if block_index >= len(blocks):
            break
    return write_zip_with_replacement(content, replacements)


def extract_odt_text_from_bytes(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as odt:
            ensure_zip_size(odt)
            document = odt.read("content.xml")
    except HTTPException:
        raise
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


def export_odt_with_translated_text(content: bytes, translated_text: str) -> bytes:
    blocks = translated_blocks(translated_text)
    if not blocks:
        raise HTTPException(status_code=400, detail="No translated text to export")
    try:
        with zipfile.ZipFile(BytesIO(content)) as odt:
            ensure_zip_size(odt)
            document = odt.read("content.xml")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read ODT: {exc}") from exc

    namespaces = {"text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    root = ElementTree.fromstring(document)
    block_index = 0
    for paragraph in root.findall(".//text:p", namespaces):
        if not "".join(paragraph.itertext()).strip():
            continue
        if block_index >= len(blocks):
            break
        replace_text_preserving_markup(paragraph, blocks[block_index])
        block_index += 1
    return write_zip_with_replacement(content, {"content.xml": ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)})


def pptx_text_part_names(pptx: zipfile.ZipFile) -> List[str]:
    names = [
        item.filename
        for item in pptx.infolist()
        if item.filename.startswith("ppt/slides/slide") and item.filename.endswith(".xml")
    ]
    return sorted(names, key=lambda name: [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)])


def extract_pptx_text_from_bytes(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as pptx:
            ensure_zip_size(pptx)
            documents = {part: pptx.read(part) for part in pptx_text_part_names(pptx)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PPTX: {exc}") from exc

    namespaces = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    paragraphs = []
    for part, document in documents.items():
        try:
            root = ElementTree.fromstring(document)
        except ElementTree.ParseError as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse PPTX XML {part}: {exc}") from exc
        for paragraph in root.findall(".//a:p", namespaces):
            text = "".join(node.text or "" for node in paragraph.findall(".//a:t", namespaces)).strip()
            if text:
                paragraphs.append(text)

    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in PPTX")
    return result


def export_pptx_with_translated_text(content: bytes, translated_text: str) -> bytes:
    blocks = translated_blocks(translated_text)
    if not blocks:
        raise HTTPException(status_code=400, detail="No translated text to export")
    try:
        with zipfile.ZipFile(BytesIO(content)) as pptx:
            ensure_zip_size(pptx)
            documents = {part: pptx.read(part) for part in pptx_text_part_names(pptx)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PPTX: {exc}") from exc

    namespaces = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    replacements = {}
    block_index = 0
    for part, document in documents.items():
        root = ElementTree.fromstring(document)
        changed = False
        for paragraph in root.findall(".//a:p", namespaces):
            text_nodes = paragraph.findall(".//a:t", namespaces)
            if not "".join(node.text or "" for node in text_nodes).strip():
                continue
            if block_index >= len(blocks):
                break
            for node_index, node in enumerate(text_nodes):
                node.text = blocks[block_index] if node_index == 0 else ""
            block_index += 1
            changed = True
        if changed:
            replacements[part] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        if block_index >= len(blocks):
            break
    return write_zip_with_replacement(content, replacements)


def replace_text_preserving_markup(element, text: str):
    text_written = False
    if element.text is not None:
        element.text = text
        text_written = True
    for node in element.iter():
        if node is element:
            continue
        if node.text is not None:
            node.text = "" if text_written else text
            text_written = True
        if node.tail is not None:
            node.tail = ""
    if not text_written:
        element.text = text


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


def export_csv_with_translated_text(content: bytes, columns: str, translated_text: str) -> bytes:
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

    rows = list(reader)
    blocks = translated_blocks(translated_text)
    for row, block in zip(rows, blocks):
        values = [value.strip() for value in block.split(" | ")]
        for column, value in zip(selected_columns, values):
            row[column] = value

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


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


def xlsx_cell_text(cell, shared: List[str], namespace: Dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    value_node = cell.find("s:v", namespace)
    inline_node = cell.find(".//s:t", namespace)
    if cell_type == "s" and value_node is not None:
        try:
            return shared[int(value_node.text or "0")]
        except (ValueError, IndexError) as exc:
            raise HTTPException(status_code=400, detail="Invalid XLSX shared string reference") from exc
    if inline_node is not None:
        return inline_node.text or ""
    if value_node is not None:
        return value_node.text or ""
    return ""


def xlsx_rows_with_values(root, shared: List[str], namespace: Dict[str, str]) -> List[Dict[str, Any]]:
    rows = []
    for row in root.findall(".//s:row", namespace):
        values = {}
        cells = {}
        for cell in row.findall("s:c", namespace):
            ref = cell.attrib.get("r", "")
            column = xlsx_column_name(ref)
            cells[column] = cell
            values[column] = xlsx_cell_text(cell, shared, namespace).strip()
        if values:
            rows.append({"row": row, "values": values, "cells": cells})
    return rows


def resolve_xlsx_columns(rows: List[Dict[str, Any]], selected_columns: List[str]) -> List[str]:
    if not rows:
        raise HTTPException(status_code=422, detail="XLSX sheet has no rows")
    header = {value: column for column, value in rows[0]["values"].items() if value}
    resolved_columns = [header.get(column, column.upper()) for column in selected_columns]
    missing = [column for column in resolved_columns if all(not row["values"].get(column) for row in rows)]
    if missing:
        raise HTTPException(status_code=400, detail="XLSX columns not found: " + ", ".join(missing))
    return resolved_columns


def extract_xlsx_text_from_bytes(content: bytes, sheet_name: str, columns: str) -> str:
    selected_columns = parse_column_names(columns)
    if not selected_columns:
        raise HTTPException(status_code=400, detail="Select at least one XLSX column")

    try:
        workbook = zipfile.ZipFile(BytesIO(content))
        ensure_zip_size(workbook)
    except HTTPException:
        raise
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
    rows = xlsx_rows_with_values(root, shared, namespace)
    resolved_columns = resolve_xlsx_columns(rows, selected_columns)

    output_rows = []
    for row in rows[1:]:
        values = [row["values"].get(column, "").strip() for column in resolved_columns]
        line = " | ".join(value for value in values if value)
        if line:
            output_rows.append(line)

    result = "\n\n".join(output_rows).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in selected XLSX columns")
    return result


def xlsx_set_cell_text(cell, text: str):
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    formula = cell.find(f"{namespace}f")
    if formula is not None:
        cell.attrib.pop("t", None)
        for child in list(cell):
            if child.tag != f"{namespace}f":
                cell.remove(child)
        value_node = ElementTree.SubElement(cell, f"{namespace}v")
        value_node.text = text
        return
    cell.attrib["t"] = "inlineStr"
    for child in list(cell):
        cell.remove(child)
    inline = ElementTree.SubElement(cell, f"{namespace}is")
    node = ElementTree.SubElement(inline, f"{namespace}t")
    node.text = text


def export_xlsx_with_translated_text(content: bytes, sheet_name: str, columns: str, translated_text: str) -> bytes:
    selected_columns = parse_column_names(columns)
    if not selected_columns:
        raise HTTPException(status_code=400, detail="Select at least one XLSX column")
    try:
        workbook = zipfile.ZipFile(BytesIO(content))
        ensure_zip_size(workbook)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read XLSX: {exc}") from exc

    try:
        with workbook:
            shared = xlsx_shared_strings(workbook)
            sheet_path = xlsx_sheet_path(workbook, sheet_name)
            sheet_xml = workbook.read(sheet_path)
            root = ElementTree.fromstring(sheet_xml)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse XLSX: {exc}") from exc

    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = xlsx_rows_with_values(root, shared, namespace)
    resolved_columns = resolve_xlsx_columns(rows, selected_columns)
    blocks = translated_blocks(translated_text)
    for row_info, block in zip(rows[1:], blocks):
        values = [value.strip() for value in block.split(" | ")]
        for column, value in zip(resolved_columns, values):
            cell = row_info["cells"].get(column)
            if cell is not None:
                xlsx_set_cell_text(cell, value)

    return write_zip_with_replacement(content, {sheet_path: ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)})


class TranslatableHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.output = []
        self.texts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        self.output.append(self.get_starttag_text())
        if tag.lower() in {"script", "style"}:
            self._skip_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.output.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        self.output.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip_depth or not data.strip():
            self.output.append(data)
            return
        index = len(self.texts)
        self.texts.append(data.strip())
        prefix = data[:len(data) - len(data.lstrip())]
        suffix = data[len(data.rstrip()):]
        self.output.append(("text", index, prefix, suffix))

    def handle_entityref(self, name):
        self.output.append(f"&{name};")

    def handle_charref(self, name):
        self.output.append(f"&#{name};")

    def handle_comment(self, data):
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.output.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.output.append(f"<?{data}>")


def parse_html(content: bytes) -> TranslatableHtmlParser:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode HTML as UTF-8: {exc}") from exc
    parser = TranslatableHtmlParser()
    parser.feed(text)
    parser.close()
    return parser


def extract_html_text_from_bytes(content: bytes) -> str:
    parser = parse_html(content)
    result = "\n\n".join(parser.texts).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in HTML")
    return result


def export_html_with_translated_text(content: bytes, translated_text: str) -> bytes:
    parser = parse_html(content)
    blocks = translated_blocks(translated_text)
    rendered = []
    for part in parser.output:
        if isinstance(part, tuple):
            _, index, prefix, suffix = part
            rendered.append(prefix + (blocks[index] if index < len(blocks) else parser.texts[index]) + suffix)
        else:
            rendered.append(part)
    return "".join(rendered).encode("utf-8")


def subtitle_text_blocks(text: str) -> List[Tuple[int, str]]:
    blocks = []
    for match in re.finditer(r"(?ms)(^|\n)([^\n]*-->\s*[^\n]+)\n(.*?)(?=\n\s*\n|\Z)", text):
        cue_text = "\n".join(line for line in match.group(3).splitlines() if line.strip() and not line.lstrip().startswith(("NOTE", "STYLE", "REGION")))
        if cue_text.strip():
            blocks.append((match.start(3), cue_text.strip()))
    return blocks


def extract_subtitle_text_from_bytes(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode subtitle as UTF-8: {exc}") from exc
    blocks = [block for _, block in subtitle_text_blocks(text)]
    result = "\n\n".join(blocks).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No subtitle text found")
    return result


def export_subtitle_with_translated_text(content: bytes, translated_text: str) -> bytes:
    try:
        original = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode subtitle as UTF-8: {exc}") from exc
    blocks = translated_blocks(translated_text)
    parts = re.split(r"(\n\s*\n)", original)
    block_index = 0
    for index, part in enumerate(parts):
        if "-->" not in part or block_index >= len(blocks):
            continue
        lines = part.splitlines()
        cue_line = next((line_index for line_index, line in enumerate(lines) if "-->" in line), None)
        if cue_line is None:
            continue
        parts[index] = "\n".join(lines[:cue_line + 1] + blocks[block_index].splitlines())
        block_index += 1
    return "".join(parts).encode("utf-8")


def json_string_paths(value: Any, path: Tuple[Any, ...] = ()) -> List[Tuple[Tuple[Any, ...], str]]:
    if isinstance(value, str):
        return [(path, value)] if value.strip() else []
    if isinstance(value, list):
        paths = []
        for index, item in enumerate(value):
            paths.extend(json_string_paths(item, path + (index,)))
        return paths
    if isinstance(value, dict):
        paths = []
        for key, item in value.items():
            paths.extend(json_string_paths(item, path + (key,)))
        return paths
    return []


def set_path_value(value: Any, path: Tuple[Any, ...], replacement: str):
    target = value
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement


def extract_json_text_from_bytes(content: bytes) -> str:
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse JSON: {exc}") from exc
    values = [text for _, text in json_string_paths(data)]
    result = "\n\n".join(values).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No translatable strings found in JSON")
    return result


def export_json_with_translated_text(content: bytes, translated_text: str) -> bytes:
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse JSON: {exc}") from exc
    paths = json_string_paths(data)
    for (path, _), block in zip(paths, translated_blocks(translated_text)):
        set_path_value(data, path, block)
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


YAML_SCALAR_RE = re.compile(r"^(\s*[-\w\"'].*?:\s*)(['\"]?)([^#\n]*?\S)(\2)(\s*(?:#.*)?)$")


def yaml_scalar_lines(text: str) -> List[Tuple[int, str]]:
    scalars = []
    for index, line in enumerate(text.splitlines()):
        match = YAML_SCALAR_RE.match(line)
        if match and match.group(3).strip() not in {"true", "false", "null", "~"} and not re.fullmatch(r"[-+]?\d+(\.\d+)?", match.group(3).strip()):
            scalars.append((index, match.group(3).strip()))
    return scalars


def extract_yaml_text_from_bytes(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode YAML as UTF-8: {exc}") from exc
    values = [value for _, value in yaml_scalar_lines(text)]
    result = "\n\n".join(values).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No simple YAML strings found")
    return result


def export_yaml_with_translated_text(content: bytes, translated_text: str) -> bytes:
    text = content.decode("utf-8-sig")
    lines = text.splitlines()
    blocks = translated_blocks(translated_text)
    for (line_index, _), block in zip(yaml_scalar_lines(text), blocks):
        match = YAML_SCALAR_RE.match(lines[line_index])
        if match:
            lines[line_index] = match.group(1) + match.group(2) + block + match.group(4) + match.group(5)
    return ("\n".join(lines) + ("\n" if text.endswith("\n") else "")).encode("utf-8")


def po_entries(text: str) -> List[Tuple[str, str]]:
    entries = []
    current_id = None
    for line in text.splitlines():
        if line.startswith("msgid "):
            current_id = po_unquote(line[6:].strip())
        elif line.startswith("msgstr ") and current_id:
            entries.append((current_id, po_unquote(line[7:].strip())))
            current_id = None
    return entries


def po_unquote(value: str) -> str:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip('"')


def po_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def extract_po_text_from_bytes(content: bytes) -> str:
    text = content.decode("utf-8-sig")
    values = [msgstr or msgid for msgid, msgstr in po_entries(text) if (msgstr or msgid).strip()]
    result = "\n\n".join(values).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No PO messages found")
    return result


def export_po_with_translated_text(content: bytes, translated_text: str) -> bytes:
    lines = content.decode("utf-8-sig").splitlines()
    blocks = translated_blocks(translated_text)
    block_index = 0
    for index, line in enumerate(lines):
        if line.startswith("msgstr ") and block_index < len(blocks):
            lines[index] = "msgstr " + po_quote(blocks[block_index])
            block_index += 1
    return ("\n".join(lines) + "\n").encode("utf-8")


def extract_xliff_text_from_bytes(content: bytes) -> str:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse XLIFF: {exc}") from exc
    values = []
    for unit in root.findall(".//{*}trans-unit"):
        target = unit.find("{*}target")
        source = unit.find("{*}source")
        text = "".join(target.itertext()).strip() if target is not None else ""
        if not text and source is not None:
            text = "".join(source.itertext()).strip()
        if text:
            values.append(text)
    result = "\n\n".join(values).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No XLIFF text found")
    return result


def xml_tag_with_namespace(parent, local_name: str) -> str:
    if parent.tag.startswith("{"):
        namespace, _, _ = parent.tag[1:].partition("}")
        return "{" + namespace + "}" + local_name
    return local_name


def export_xliff_with_translated_text(content: bytes, translated_text: str) -> bytes:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse XLIFF: {exc}") from exc
    blocks = translated_blocks(translated_text)
    block_index = 0
    for unit in root.findall(".//{*}trans-unit"):
        target = unit.find("{*}target")
        if target is None:
            target = ElementTree.SubElement(unit, xml_tag_with_namespace(unit, "target"))
        if block_index >= len(blocks):
            break
        replace_text_preserving_markup(target, blocks[block_index])
        block_index += 1
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


async def extract_pdf_markdown(file: UploadFile, page_range: str = "", use_ocr: bool = False) -> str:
    content = await read_upload_bytes(file, "PDF")
    return extract_pdf_markdown_from_bytes(content, file.content_type or "application/pdf", page_range, use_ocr=use_ocr)


def run_text_job(
    job_id: str,
    text: str,
    source: str,
    target: str,
    original_name: str = "text",
    source_content: bytes = b"",
    source_extension: str = "",
    source_meta: Optional[Dict[str, str]] = None,
):
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
        history_id = save_history(
            "text",
            result,
            source,
            target,
            original_name,
            source_content,
            source_extension,
            source_meta,
        )
        update_job(
            job_id,
            status="complete",
            message="Complete",
            current=len(chunks),
            result=result,
            history_id=history_id,
            finished_at=time.time(),
        )
        cleanup_job_payload(job_id)
    except Exception as exc:
        if str(exc) == "Job stopped by user":
            update_job(job_id, status="cancelled", message="Cancelled", error=None, finished_at=time.time())
        else:
            update_job(job_id, status="failed", message="Failed", error=exception_message(exc), finished_at=time.time())
        cleanup_job_payload(job_id)


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
    use_ocr: bool = False,
):
    try:
        update_job(job_id, status="running", message="Extracting PDF", started_at=time.time())
        markdown = extract_pdf_markdown_from_bytes(content, content_type, page_range, use_ocr=use_ocr)
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
        history_id = save_history("pdf", result, source, target, filename, content, "pdf")
        update_job(
            job_id,
            status="complete",
            message="Complete",
            current=total,
            result=result,
            history_id=history_id,
            finished_at=time.time(),
        )
        cleanup_job_payload(job_id)
    except Exception as exc:
        if str(exc) == "Job stopped by user":
            update_job(job_id, status="cancelled", message="Cancelled", error=None, finished_at=time.time())
        else:
            update_job(job_id, status="failed", message="Failed", error=exception_message(exc), finished_at=time.time())
        cleanup_job_payload(job_id)


def run_pdf_layout_translate_job(
    job_id: str,
    content: bytes,
    source: str,
    target: str,
    filename: str,
):
    try:
        update_job(job_id, status="running", message="Reading PDF layout", started_at=time.time())
        pages = extract_pdf_layout(content)
        wait_if_paused_or_cancelled(job_id)
        paragraphs = [paragraph["text"] for page in pages for paragraph in page["paragraphs"]]
        # One paragraph per chunk: the overlay maps translations back to paragraphs by position,
        # and the model does not reliably keep paragraph breaks inside a single chunk.
        chunks: List[str] = []
        chunk_counts: List[int] = []
        for paragraph in paragraphs:
            parts = split_long_text(paragraph, MAX_CHARS)
            chunks.extend(parts)
            chunk_counts.append(len(parts))
        update_job(job_id, total=len(chunks), message=f"Translating 0 / {len(chunks)} chunks")
        translated_chunks = translate_chunks(chunks, source, target, job_id)

        translated: List[str] = []
        position = 0
        for count in chunk_counts:
            # Collapsed to a single line so the paragraph split on re-export stays exact.
            translated.append(re.sub(r"\s+", " ", " ".join(translated_chunks[position:position + count])).strip())
            position += count
        result = "\n\n".join(translated)
        history_id = save_history(
            "translate-pdf-layout",
            result,
            source,
            target,
            filename,
            content,
            "pdf",
            {"layout": "true"},
        )
        update_job(
            job_id,
            status="complete",
            message="Complete",
            current=len(chunks),
            result=result,
            history_id=history_id,
            finished_at=time.time(),
        )
        cleanup_job_payload(job_id)
    except Exception as exc:
        if str(exc) == "Job stopped by user":
            update_job(job_id, status="cancelled", message="Cancelled", error=None, finished_at=time.time())
        else:
            update_job(job_id, status="failed", message="Failed", error=exception_message(exc), finished_at=time.time())
        cleanup_job_payload(job_id)


@app.get("/health")
def health():
    ocr_available = bool(shutil.which("pdftoppm") and shutil.which("tesseract"))
    return {
        "status": "ok",
        "model": MODEL_ID,
        "device": selected_device(),
        "max_chars": MAX_CHARS,
        "max_file_mb": MAX_FILE_MB,
        "ocr_enabled": OCR_ENABLED,
        "ocr_available": ocr_available,
        "ocr_language": OCR_LANGUAGE,
        "model_idle_unload_enabled": MODEL_IDLE_UNLOAD_ENABLED,
        "model_idle_seconds": MODEL_IDLE_SECONDS,
        "job_workers": JOB_WORKERS,
        "cpu_threads": CPU_THREADS,
        "cpu_interop_threads": CPU_INTEROP_THREADS,
        "model_loaded": model_cache_loaded(),
        "root_path": ROOT_PATH,
        "public_url": PUBLIC_URL,
        "trust_proxy_headers": TRUST_PROXY_HEADERS,
    }


@app.get("/languages")
def languages():
    return {
        "source_default": DEFAULT_SOURCE,
        "target_default": DEFAULT_TARGET,
        "languages": [{"code": code, "name": code} for code in language_codes()],
    }


@app.get("/jobs")
def jobs_status():
    ensure_queue_workers()
    return {"workers": JOB_WORKERS, "items": list_jobs()}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    return get_job(job_id)


@app.post("/jobs/{job_id}/{action}")
def job_control(job_id: str, action: str):
    return control_job(job_id, action)


@app.post("/jobs/translate")
def start_translate_job(request: TranslateRequest):
    ensure_queue_workers()
    text = "\n\n".join(str(item) for item in request.q) if isinstance(request.q, list) else str(request.q)
    job_id = create_job("translate", request.source, request.target, "Text")
    update_job(job_id, text=text)
    register_job_runner(job_id, run_text_job, (job_id, text, request.source, request.target))
    return {"job_id": job_id}


@app.post("/jobs/translate-file")
async def start_translate_file_job(
    file: UploadFile = File(...),
    text: str = Form(""),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
    columns: str = Form(""),
    sheet_name: str = Form(""),
):
    ensure_queue_workers()
    content = await read_upload_bytes(file, "Source file")
    filename = file.filename or "source"
    extension = file_extension(filename)
    job_id = create_job("translate", source, target, filename)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    source_payload_path = job_payload_path(job_id).with_suffix(".source")
    source_payload_path.write_bytes(content)
    source_meta = {"columns": columns, "sheet_name": sheet_name}
    update_job(
        job_id,
        text=text,
        original_name=filename,
        source_extension=extension,
        source_payload_path=str(source_payload_path),
        source_meta=source_meta,
    )
    register_job_runner(
        job_id,
        run_text_job,
        (job_id, text, source, target, filename, content, extension, source_meta),
    )
    return {"job_id": job_id}


@app.post("/jobs/translate-pdf")
async def start_translate_pdf_job(
    file: UploadFile = File(...),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
    page_range: str = Form(""),
):
    ensure_queue_workers()
    content = await read_upload_bytes(file, "PDF")
    filename = file.filename or "pdf"
    job_id = create_job("translate-pdf", source, target, filename)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = job_payload_path(job_id)
    payload_path.write_bytes(content)
    update_job(
        job_id,
        payload_path=str(payload_path),
        content_type=file.content_type or "application/pdf",
        filename=filename,
        page_range=page_range,
        use_ocr=OCR_ENABLED,
    )
    register_job_runner(
        job_id,
        run_pdf_translate_job,
        (job_id, content, file.content_type or "application/pdf", source, target, filename, page_range, OCR_ENABLED),
    )
    return {"job_id": job_id}


@app.post("/jobs/translate-pdf-layout")
async def start_translate_pdf_layout_job(
    file: UploadFile = File(...),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
):
    ensure_queue_workers()
    content = await read_upload_bytes(file, "PDF")
    filename = file.filename or "pdf"
    job_id = create_job("translate-pdf-layout", source, target, filename)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = job_payload_path(job_id)
    payload_path.write_bytes(content)
    update_job(job_id, payload_path=str(payload_path), filename=filename)
    register_job_runner(
        job_id,
        run_pdf_layout_translate_job,
        (job_id, content, source, target, filename),
    )
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
        return export_docx_with_translated_text(content, text)
    if extension == "odt":
        return export_odt_with_translated_text(content, text)
    if extension == "pptx":
        return export_pptx_with_translated_text(content, text)
    if extension == "csv":
        return export_csv_with_translated_text(content, source_meta.get("columns", ""), text)
    if extension == "xlsx":
        return export_xlsx_with_translated_text(content, source_meta.get("sheet_name", ""), source_meta.get("columns", ""), text)
    if extension in ("html", "htm"):
        return export_html_with_translated_text(content, text)
    if extension in ("srt", "vtt"):
        return export_subtitle_with_translated_text(content, text)
    if extension == "json":
        return export_json_with_translated_text(content, text)
    if extension in ("yaml", "yml"):
        return export_yaml_with_translated_text(content, text)
    if extension == "po":
        return export_po_with_translated_text(content, text)
    if extension in ("xlf", "xliff"):
        return export_xliff_with_translated_text(content, text)
    if extension == "pdf":
        if source_meta.get("layout") == "true":
            return export_pdf_layout_with_translated_text(content, text)
        return create_text_pdf(text)
    if extension in ("md", "txt"):
        return text.encode("utf-8")
    raise HTTPException(status_code=400, detail="Unsupported history original format")


@app.get("/history/{item_id}/export")
def export_history(item_id: str, format: str = "md"):
    path, _ = history_paths(item_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    text = path.read_text(encoding="utf-8")
    safe_format = format.lower()
    filename_base = path.stem
    if safe_format == "md":
        return FileResponse(path, media_type="text/markdown", filename=path.name)
    if safe_format == "txt":
        return Response(
            text,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.txt"'},
        )
    if safe_format == "pdf":
        return Response(
            create_text_pdf(text),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'},
        )
    item = history_item(item_id)
    source_extension = file_extension("x." + item.get("source_extension", ""))
    requested_extension = source_extension if safe_format == "original" else file_extension("x." + safe_format)
    source_path = history_source_path(item_id, source_extension) if source_extension else None
    if not source_path or not source_path.exists():
        raise HTTPException(status_code=404, detail="History source file not found")
    if requested_extension != source_extension:
        raise HTTPException(status_code=400, detail="History source can only be exported in its original format")
    source_meta = item.get("source_meta", {})
    content = export_original_history_content(
        source_extension,
        source_path.read_bytes(),
        text,
        source_meta,
    )
    return Response(
        content,
        media_type=original_export_media_type(source_extension),
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.{source_extension}"'},
    )


@app.post("/export-pdf")
def export_pdf(request: PdfExportRequest):
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="No text to export")
    return Response(
        create_text_pdf(request.text),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.pdf"'},
    )


@app.post("/export-docx")
async def export_docx(
    file: UploadFile = File(...),
    text: str = Form(""),
):
    content = await read_upload_bytes(file, "DOCX")
    return Response(
        export_docx_with_translated_text(content, text),
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
        export_odt_with_translated_text(content, text),
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
        export_pptx_with_translated_text(content, text),
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
    return Response(
        export_csv_with_translated_text(content, columns, text),
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
    return Response(
        export_xlsx_with_translated_text(content, sheet_name, columns, text),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="linguinator-translation.xlsx"'},
    )


@app.post("/export-html")
async def export_html(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "HTML")
    return Response(export_html_with_translated_text(content, text), media_type="text/html")


@app.post("/export-subtitle")
async def export_subtitle(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "Subtitle")
    return Response(export_subtitle_with_translated_text(content, text), media_type="text/plain")


@app.post("/export-json")
async def export_json(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "JSON")
    return Response(export_json_with_translated_text(content, text), media_type="application/json")


@app.post("/export-yaml")
async def export_yaml(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "YAML")
    return Response(export_yaml_with_translated_text(content, text), media_type="text/yaml")


@app.post("/export-po")
async def export_po(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "PO")
    return Response(export_po_with_translated_text(content, text), media_type="text/plain")


@app.post("/export-xliff")
async def export_xliff(file: UploadFile = File(...), text: str = Form("")):
    content = await read_upload_bytes(file, "XLIFF")
    return Response(export_xliff_with_translated_text(content, text), media_type="application/xml")


@app.delete("/history/{item_id}")
def delete_history(item_id: str):
    md_path, json_path = history_paths(item_id)
    if not md_path.exists() and not json_path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    source_path = None
    if json_path.exists():
        try:
            item = json.loads(json_path.read_text(encoding="utf-8"))
            source_extension = file_extension("x." + item.get("source_extension", ""))
            if source_extension:
                source_path = history_source_path(item_id, source_extension)
        except Exception:
            source_path = None
    md_path.unlink(missing_ok=True)
    json_path.unlink(missing_ok=True)
    if source_path:
        source_path.unlink(missing_ok=True)
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
    return await extract_pdf_markdown(file, page_range, OCR_ENABLED)


@app.post("/extract-docx", response_class=PlainTextResponse)
async def extract_docx(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "DOCX")
    return extract_docx_text_from_bytes(content)


@app.post("/extract-odt", response_class=PlainTextResponse)
async def extract_odt(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "ODT")
    return extract_odt_text_from_bytes(content)


@app.post("/extract-pptx", response_class=PlainTextResponse)
async def extract_pptx(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "PPTX")
    return extract_pptx_text_from_bytes(content)


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


@app.post("/extract-html", response_class=PlainTextResponse)
async def extract_html(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "HTML")
    return extract_html_text_from_bytes(content)


@app.post("/extract-subtitle", response_class=PlainTextResponse)
async def extract_subtitle(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "Subtitle")
    return extract_subtitle_text_from_bytes(content)


@app.post("/extract-json", response_class=PlainTextResponse)
async def extract_json(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "JSON")
    return extract_json_text_from_bytes(content)


@app.post("/extract-yaml", response_class=PlainTextResponse)
async def extract_yaml(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "YAML")
    return extract_yaml_text_from_bytes(content)


@app.post("/extract-po", response_class=PlainTextResponse)
async def extract_po(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "PO")
    return extract_po_text_from_bytes(content)


@app.post("/extract-xliff", response_class=PlainTextResponse)
async def extract_xliff(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "XLIFF")
    return extract_xliff_text_from_bytes(content)


@app.post("/translate-pdf", response_class=PlainTextResponse)
async def translate_pdf(
    file: UploadFile = File(...),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
    page_range: str = Form(""),
):
    markdown = await extract_pdf_markdown(file, page_range, OCR_ENABLED)
    translated = []
    for section in re.split(r"(?m)^# Page ", markdown):
        section = section.strip()
        if not section:
            continue
        page_number, _, page_text = section.partition("\n")
        translated.append(f"# Page {page_number.strip()}\n\n{translate_text(page_text.strip(), source, target)}")
    return "\n\n".join(translated)

