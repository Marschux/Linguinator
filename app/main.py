# ... existing imports and setup code ...
import base64
import ctypes
import gc
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
import unicodedata
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import lru_cache
from html.parser import HTMLParser
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union
# lxml, not the stdlib xml.etree.ElementTree it's a drop-in replacement for here: a real
# Office-authored file's mc:Ignorable="x14ac xr xr2 xr3" names namespace prefixes by that literal
# string, and stdlib ElementTree renames every prefix to its own ns0/ns1/... on serialization,
# leaving mc:Ignorable pointing at prefixes that no longer exist - Excel refuses to open the
# result ("file is damaged"), measured on a real spreadsheet round-tripped through export. lxml
# preserves the original prefixes, so the reference stays valid. lxml.etree.ParseError is a base
# class of its XMLSyntaxError, so every existing `except ElementTree.ParseError` still catches it.
from lxml import etree as ElementTree

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from langdetect import DetectorFactory, LangDetectException, detect
from pydantic import BaseModel
# Text extraction runs on PyMuPDF (it decodes subset fonts pypdf silently drops words from and
# reports position, size and bold in one pass); pypdf still does the overlay merge.
import pymupdf
from pypdf import PdfReader, PdfWriter

DetectorFactory.seed = 0  # deterministic detection results across runs


def env_value(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Not configurable: another model can carry another licence, and other model families expect
# other language tokens, which does not fail, it translates into the wrong language.
FALLBACK_MODEL_ID = "Helsinki-NLP/opus-mt-tc-bible-big-mul-mul"
# One model resident at a time: each costs 0.5-1 GB, and the queue runs one job anyway.
MODEL_CACHE_SIZE = 1
# Characters per chunk. Not a model limit, every chunk is split into single sentences before
# translation; this bounds how many of them land in one batched call, and with it peak memory.
MAX_CHARS = 2000
MAX_FILE_MB = int(env_value("LINGUINATOR_MAX_FILE_MB", "50"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_ZIP_UNCOMPRESSED_BYTES = MAX_FILE_BYTES * 10
# In hours, because a day is a coarse setting for a workbench whose history is a convenience,
# not an archive. LINGUINATOR_HISTORY_DAYS is still read where the new one is unset, so an
# existing .env does not silently start meaning something else.
HISTORY_HOURS = int(env_value("LINGUINATOR_HISTORY_HOURS", "")
                    or int(env_value("LINGUINATOR_HISTORY_DAYS", "0") or 0) * 24
                    or 24)
# Fixed, because the compose volume is mounted here: a different path would write into the
# container filesystem and be gone with the next restart.
HISTORY_DIR = Path("/data/history")
JOBS_DIR = HISTORY_DIR / "jobs"
HISTORY_TIMEZONE = env_value("LINGUINATOR_TIMEZONE", "Europe/Berlin")
# "12h" or "24h", the same for every UI language. Anything else falls back to 24h rather than
# failing, so an outdated .env (this used to accept "auto") does not stop the container.
TIME_FORMAT = "12h" if env_value("LINGUINATOR_TIME_FORMAT", "24h").strip() == "12h" else "24h"
# One job at a time: two jobs on different language pairs would each want their own model.
JOB_WORKERS = 1
CPU_THREADS = int(env_value("LINGUINATOR_CPU_THREADS", "0"))
# What the UI starts with; a browser that has been switched keeps its own choice.
UI_LANGUAGE = env_value("LINGUINATOR_UI_LANGUAGE", "en")
# Where detection lands when it fails. Not the UI's preselection, which is auto-detect.
DEFAULT_SOURCE = "eng_Latn"
DEFAULT_TARGET = env_value("LINGUINATOR_DEFAULT_TARGET", "eng_Latn")
AUTH_ENABLED = env_value("LINGUINATOR_AUTH_ENABLED", "false").lower() in ("1", "true", "yes", "on")
AUTH_USERNAME = env_value("LINGUINATOR_AUTH_USERNAME", "Translator")
AUTH_PASSWORD = env_value("LINGUINATOR_AUTH_PASSWORD", "")
MODEL_IDLE_SECONDS = int(env_value("LINGUINATOR_MODEL_IDLE_SECONDS", "600"))
PDF_LOW_TEXT_CHARS = 20
PDF_PAGE_WIDTH = 595
PDF_PAGE_HEIGHT = 842
PDF_MARGIN = 54
PDF_LINE_HEIGHT = 14
PDF_FONT_SIZE = 11
PDF_HEADING_FONT_SIZE = 15
PDF_FOOTER_FONT_SIZE = 9
# Below this, an image is a bullet icon or a decorative divider, not something worth reinserting
# into the re-exported translation or reserving text-wrap space for.
PDF_IMAGE_MIN_SIZE = 24.0
# A gap narrower than this cannot hold a real line of text - text falls back to full width below
# the image instead of one word per line squeezed beside it.
PDF_IMAGE_MIN_TEXT_BAND = 80.0
# How far the overlay may shrink the font to make a longer translation fit its original lines.
PDF_LAYOUT_MIN_SCALE = 0.7
# Kept clear of the page edge when a paragraph has nothing to its right.
PDF_LAYOUT_EDGE_MARGIN = 20.0
# How far the first line of a paragraph may be indented past the ones under it and still count
# as part of it. Half an inch is the usual tab; a centred heading sits much further in than that
# and has to stay a paragraph of its own.
PDF_LAYOUT_MAX_INDENT = 40.0
# How far a word gap may be stretched to justify a line, as a multiple of the font's own space.
# Re-measured 14.08.2026 (the previous value, 3.0, came apart in production at 2.95 on
# Mixed_Languages.pdf): read back after drawing, a line set word by word breaks into one span per
# word at a strikingly constant factor of 2.88, independent of font size - swept 6pt to 32pt in
# 0.01 steps against real German words, same breakpoint every time to two decimals, so this is
# MuPDF's own span-grouping heuristic and not a per-size effect the old "about 3.9" measurement
# missed. Wider gaps also tear holes into the setting, so a line that would need more stays ragged.
PDF_JUSTIFY_MAX_SPACE = 2.8
# A line opening with one of these is a list item of its own, however it is placed.
PDF_LIST_MARKER = re.compile(r"^\s*(?:[-•‣▪●◦*]|\(?\d{1,3}[.)])\s")
# How far a paragraph may be tightened purely to keep the original's line count. 0.9 because no
# paragraph in the test documents needed more than that to absorb the substitute font's extra
# width; past it, an extra line is the lesser evil.
PDF_LAYOUT_TIGHTEN_SCALE = 0.9
# The same, for a paragraph with no room below it for even one more line - a table cell being the
# usual one. There the extra line does not land in a gap, it lands outside the box, so fitting the
# original line count is worth more type size than elsewhere. As low as
# PDF_LAYOUT_CROWDED_MIN_SCALE for the same reason it is: a line outside its cell is the same
# fault as a line on top of the next paragraph. At 0.6 the header cell of Powerupall's research
# table ("im Zusammenhang mit bestimmten anderen Faktoren") stopped one step short of fitting and
# hung its last word under the table, through the rule below it.
PDF_LAYOUT_BOXED_MIN_SCALE = 0.5
# And for a paragraph that would otherwise be drawn on top of the one below it. Lower than either
# of the above, because the alternative is not a cramped page but unreadable text: measured over
# the Powerupall pages reported as colliding, the four paragraphs that still ran into their
# neighbour at the ordinary minimum needed 0.54 to 0.64 to clear it.
PDF_LAYOUT_CROWDED_MIN_SCALE = 0.5
# A hard floor under both minimums above, in points rather than as a fraction of the base size.
# level_table_sizes joins a whole table into one group through its rows and columns, so on a wide
# table (Landscape_Mixed_Pages, 10 columns by 9 rows) a single cramped cell - one long word in a
# narrow column, crowded by the row below - drags every other cell down with it: measured, an 8.5pt
# table came back at a uniform 4.2pt, technically consistent but no longer legible. Below this, a
# cell overflows its row instead of shrinking further - recoverable by the reader, where six-point
# text across a whole table is not.
PDF_LAYOUT_ABSOLUTE_MIN_SIZE = 6.0
# A reflow taller than this multiple of the paragraph it replaces is not laid out at all; the
# original stays instead. Overflowing by a line or two is normal (German runs longer, and the
# substitute font wider), a block three times the height is the model having invented text, and
# it lands on top of everything below it.
PDF_LAYOUT_MAX_LINE_GROWTH = 3
# How much of a paragraph's own width a shape has to span before paragraph_width_limit accepts it
# as the box the paragraph sits in. Measured (Aug 2026) over both documents that depend on this:
# the icons and decorations Get_Started_With_Smallpdf mistook for boxes span 0.07 to 0.38 of their
# paragraph, the real table cells of Systemrequirements 0.74 (the "Any graphic card..." line, whose
# original overruns its own cell) and upwards. The gap between those two is where this sits.
PDF_LAYOUT_WALL_MIN_SPAN = 0.55
# How far a line may sit off a centre and still count as set around it, as a fraction of the text
# column's width. Measured (Aug 2026) on Powerupall, whose text column runs 65..551: every centred
# heading and page number on it sits within 2pt of the column's centre (0.4 %), while the nearest
# thing that must not be mistaken for one, a left-aligned list item, is 120pt off (25 %).
PDF_LAYOUT_CENTRE_TOLERANCE = 0.03
# Layout-PDF paragraphs are usually short, so translating one per model call wastes most of
# each call on fixed beam-search overhead. Batched via the tensor's batch dimension (not string
# concatenation), so paragraph boundaries stay exact; kept small to cap the extra padding memory
# a batch costs over a single call.
PDF_LAYOUT_BATCH_SIZE = 4
PDF_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
PDF_FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
# Layout PDFs keep the original's serif/sans character: a Times-set contract redrawn in DejaVu
# Sans reads as a different document even when every line sits in the right place.
PDF_SERIF_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "C:/Windows/Fonts/times.ttf",
)
PDF_SERIF_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "C:/Windows/Fonts/timesbd.ttf",
)
# Italic, for a paragraph the original set in it - a pulled quote, a caption, a book title on a
# line of its own. Measured over the eight test documents: 111 lines are set wholly in italic
# (Stall-Kamera-System 14 of 104, Powerupall 95 of 3504), and every one of them came back upright.
# The DejaVu files here are in fonts-dejavu-extra, not the -core package: font_file falls back to
# the upright face where they are missing, so a build without that package loses the slant rather
# than the glyphs.
PDF_FONT_ITALIC_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "C:/Windows/Fonts/ariali.ttf",
)
PDF_FONT_BOLD_ITALIC_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
    "C:/Windows/Fonts/arialbi.ttf",
)
PDF_SERIF_ITALIC_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "C:/Windows/Fonts/timesi.ttf",
)
PDF_SERIF_BOLD_ITALIC_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf",
    "C:/Windows/Fonts/timesbi.ttf",
)
# The font resources a generated PDF declares, as (bold, serif, italic).
PDF_FONT_FACES = {
    "F1": (False, False, False), "F2": (True, False, False),
    "F3": (False, True, False), "F4": (True, True, False),
    "F5": (False, False, True), "F6": (True, False, True),
    "F7": (False, True, True), "F8": (True, True, True),
}
# Serif families as they turn up in PDF font names. MuPDF's own "serifed" span flag comes from
# the font descriptor, which plenty of sans fonts set wrongly (Roboto reports serifed), so the
# name is the more reliable signal - anything calling itself "sans" wins over these markers.
PDF_SERIF_NAME_MARKERS = (
    "serif", "times", "georgia", "garamond", "cambria", "palatino", "book",
    "roman", "minion", "baskerville", "constantia", "charter", "century",
)


def pdf_font_is_serif(name: str) -> bool:
    lowered = (name or "").lower()
    if "sans" in lowered:
        return False
    return any(marker in lowered for marker in PDF_SERIF_NAME_MARKERS)
# DejaVu Sans (the default embedded PDF font) has no CJK/Arabic/Devanagari/Hebrew glyphs, so a
# translation into those scripts would otherwise render as empty boxes. Picked automatically per
# script actually present in the text being measured/drawn (detect_pdf_script), not by target
# language, so mixed-script text still renders whatever coverage the source PDF font offered.
PDF_SCRIPT_FONT_CANDIDATES = {
    "cjk": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msgothic.ttc",
    ),
    "arabic": (
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ),
    "devanagari": (
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "C:/Windows/Fonts/mangal.ttf",
        # Mangal only ships with Windows' Hindi language support; Nirmala UI is always there.
        "C:/Windows/Fonts/Nirmala.ttc",
    ),
    "hebrew": (
        "/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ),
    "thai": (
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "C:/Windows/Fonts/leelawad.ttf",
    ),
}


def detect_pdf_script(text: str) -> str:
    """Which non-Latin script (if any) a string needs a fallback font for."""
    for char in text:
        codepoint = ord(char)
        if 0x4E00 <= codepoint <= 0x9FFF or 0x3040 <= codepoint <= 0x30FF or 0xAC00 <= codepoint <= 0xD7A3 or 0x3400 <= codepoint <= 0x4DBF:
            return "cjk"
        if 0x0600 <= codepoint <= 0x06FF or 0x0750 <= codepoint <= 0x077F:
            return "arabic"
        if 0x0900 <= codepoint <= 0x097F:
            return "devanagari"
        if 0x0590 <= codepoint <= 0x05FF:
            return "hebrew"
        if 0x0E00 <= codepoint <= 0x0E7F:
            return "thai"
    return ""
# The language picker's contents, and simultaneously the alias table used to look up OPUS-MT
# pair models (whose ids use iso-639-1 codes) and to build the >>xxx<< prefix token for the
# multilingual fallback model. Restricted to languages with at least one bilingual OPUS-MT
# model against English.
CORE_LANGUAGES = {
    "en": "eng_Latn", "de": "deu_Latn", "fr": "fra_Latn", "es": "spa_Latn", "it": "ita_Latn",
    "nl": "nld_Latn", "pt": "por_Latn", "pl": "pol_Latn", "ru": "rus_Cyrl", "uk": "ukr_Cyrl",
    "sv": "swe_Latn", "da": "dan_Latn", "fi": "fin_Latn", "el": "ell_Grek",
    "hu": "hun_Latn", "bg": "bul_Cyrl",
    "zh": "zho_Hans", "ja": "jpn_Jpan",
    "tr": "tur_Latn",
    # No dedicated pair model exists for Latin, and langdetect cannot name it, so auto-detect will
    # read a Latin source as Italian or Romanian - it has to be set by hand. The fallback answers
    # in Latin (>>lat<< is in its vocabulary, checked) but reaches for the vocabulary of its bible
    # training: "Der Hund schlaeft im Garten" came back as "Canis dormit in jardine".
    "la": "lat_Latn",
}
INTERNAL_TO_ISO_639_1 = {internal: short for short, internal in CORE_LANGUAGES.items()}

# The favourites at the top of both language pickers. English is always among them, so the
# setting names the other three at most: more than four entries turn the shortcut back into
# the long list it is meant to shorten.
FAVORITE_LANGUAGE_LIMIT = 3
FIXED_FAVORITE_LANGUAGE = "eng_Latn"


def parse_favorite_languages(value: str) -> List[str]:
    favorites: List[str] = []
    for entry in value.split(","):
        code = entry.strip()
        if code in INTERNAL_TO_ISO_639_1 and code not in favorites:
            favorites.append(code)
        if len(favorites) >= FAVORITE_LANGUAGE_LIMIT:
            break
    if FIXED_FAVORITE_LANGUAGE not in favorites:
        favorites.append(FIXED_FAVORITE_LANGUAGE)
    return favorites


FAVORITE_LANGUAGES = parse_favorite_languages(
    env_value("LINGUINATOR_FAVORITE_LANGUAGES", "deu_Latn,spa_Latn,fra_Latn")
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
ElementTree.register_namespace("style", "urn:oasis:names:tc:opendocument:xmlns:style:1.0")
ElementTree.register_namespace("fo", "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0")

APP_DIR = Path(__file__).resolve().parent


def normalized_root_path(value: str) -> str:
    path = value.strip().strip("/")
    return f"/{path}" if path else ""


ROOT_PATH = normalized_root_path(os.getenv("LINGUINATOR_ROOT_PATH", ""))

app = FastAPI(title="Linguinator", version="1.0.6", root_path=ROOT_PATH)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
JOBS: Dict[str, Dict[str, Any]] = {}
JOB_RUNNERS: Dict[str, Tuple[Callable[..., None], Tuple[Any, ...]]] = {}
JOBS_LOCK = threading.RLock()
JOBS_CONDITION = threading.Condition(JOBS_LOCK)
QUEUE_WORKERS_STARTED = False
# Per-chunk progress ticks otherwise wrote the whole job out to disk once per sentence - a
# document with thousands of sentences did thousands of small synchronous writes purely for
# crash-resume bookkeeping. update_job skips a write within this many seconds of the last one for
# the same job, unless the job just reached a terminal status.
JOB_PERSIST_MIN_INTERVAL_SECONDS = 2.0
JOB_LAST_PERSISTED_AT: Dict[str, float] = {}
MODEL_LOCK = threading.RLock()
MODEL_ACTIVE_USERS = 0
MODEL_LAST_USED = 0.0


class TranslateRequest(BaseModel):
    q: Union[str, List[str]]
    source: str = DEFAULT_SOURCE
    target: str = DEFAULT_TARGET


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


@lru_cache(maxsize=1)
def selected_device():
    """The GPU when there is one, otherwise the CPU. Nothing to configure: asking for cuda on a
    host without a GPU only ever meant a silent fall back to cpu anyway."""
    try:
        import torch
    except ImportError:
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def configure_torch_threads(torch_module):
    if CPU_THREADS > 0 and hasattr(torch_module, "set_num_threads"):
        torch_module.set_num_threads(CPU_THREADS)


@lru_cache(maxsize=MODEL_CACHE_SIZE)
def load_tokenizer(model_id: str):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("transformers is required for translation") from exc
    return AutoTokenizer.from_pretrained(model_id)


@lru_cache(maxsize=MODEL_CACHE_SIZE)
def load_model(model_id: str):
    try:
        import torch
        from transformers import AutoModelForSeq2SeqLM
    except ImportError as exc:
        raise RuntimeError("torch and transformers are required for translation") from exc
    configure_torch_threads(torch)
    device = selected_device()
    tokenizer = load_tokenizer(model_id)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_id)
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
    with MODEL_LOCK:
        if MODEL_ACTIVE_USERS > 0:
            return False
        if not model_cache_loaded():
            return False
        load_model.cache_clear()
        load_tokenizer.cache_clear()
    gc.collect()
    try:
        # glibc keeps freed heap arenas rather than returning them to the OS, so RSS stays
        # high after unload without this - no-op on non-glibc platforms (Windows/macOS dev).
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass
    try:
        import torch as torch_module
    except ImportError:
        torch_module = None
    cuda = getattr(torch_module, "cuda", None) if torch_module else None
    if cuda and hasattr(cuda, "is_available") and hasattr(cuda, "empty_cache") and cuda.is_available():
        cuda.empty_cache()
    return True


def unload_model_if_idle(now: float | None = None) -> bool:
    if MODEL_IDLE_SECONDS <= 0:
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


if MODEL_IDLE_SECONDS > 0:
    threading.Thread(target=model_idle_unloader, daemon=True).start()


def iso_639_1(code: str) -> str:
    """Best-effort ISO 639-1 code for an internal deu_Latn-style code, used to look up OPUS-MT
    pair models and their >>xxx<< prefix tokens."""
    return INTERNAL_TO_ISO_639_1.get(code, code.split("_", 1)[0][:2])


def load_opus_pairs() -> Dict[str, Dict[str, str]]:
    """source>target -> {"model_id", "license"}, generated by tools/generate_opus_pairs.py.
    Read once at import time; the app never queries Hugging Face to find a model."""
    try:
        data = json.loads((APP_DIR / "opus_pairs.json").read_text(encoding="utf-8"))
        return data.get("pairs", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


OPUS_PAIRS = load_opus_pairs()


AUTO_SOURCE = "auto"

CORE_LANGUAGE_CODES = frozenset(CORE_LANGUAGES.values())


def ensure_known_language(code: str, allow_auto: bool = False) -> None:
    """Reject a source/target that is not one of the internal deu_Latn-style codes.

    Never checked before: an unvalidated code (the short "de" instead of "deu_Latn", say, from a
    direct API call rather than the UI's own dropdown, which always sends the right one) reached
    model_language_code with no signal the fallback model recognises, and reached the flag lookup
    in app.js the same way - languageCountries only knows the internal codes, so it fell back to
    a globe with nothing in the history record to explain why.
    """
    if allow_auto and code == AUTO_SOURCE:
        return
    if code not in CORE_LANGUAGE_CODES:
        raise HTTPException(status_code=400, detail=f"Unknown language code: {code}")


def detect_source_language(text: str) -> str:
    """Guess the internal language code (deu_Latn-style) of text, falling back to
    DEFAULT_SOURCE when detection fails or lands on a language we have no model for."""
    try:
        guess = detect(text)
    except LangDetectException:
        return DEFAULT_SOURCE
    return CORE_LANGUAGES.get(guess.split("-", 1)[0], DEFAULT_SOURCE)


def resolve_model(source: str, target: str) -> Tuple[str, bool]:
    """The model id to use for a language pair, and whether it is a dedicated bilingual model
    (True) or the multilingual fallback (False)."""
    entry = OPUS_PAIRS.get(f"{iso_639_1(source)}>{iso_639_1(target)}")
    if entry:
        return entry["model_id"], True
    return FALLBACK_MODEL_ID, False


def model_family(model_id: str) -> str:
    """Which language-signalling convention a translation model expects, keyed off its id.

    "prefix": a language token prepended to the input text, which alone picks the target
    (OPUS-MT's multilingual fallback model). "plain": no signalling at all, for a bilingual
    model that only ever translates one fixed pair.
    """
    return "prefix" if "mul-mul" in model_id.lower() else "plain"


# The multilingual fallback labels some languages by their macro-language code where our internal
# codes name a specific variety. An unknown prefix is not rejected - the tokenizer just splits it
# into ordinary subword pieces, so the model receives no target signal at all and answers in
# whatever language it likes. Any language added here must be checked against the fallback
# tokenizer's vocabulary, not assumed.
FALLBACK_LANGUAGE_ALIASES: Dict[str, str] = {}


def model_language_code(model_id: str, code: str) -> str:
    """Turn an internal deu_Latn-style code into whatever the given model expects."""
    if "mul-mul" in model_id.lower():
        short = code.split("_", 1)[0]
        return f">>{FALLBACK_LANGUAGE_ALIASES.get(short, short)}<<"
    return code


# Hard cap on input tokens, matching the smallest position-embedding size across the OPUS-MT
# models in use (512 for the bilingual pair models). Several of their tokenizer configs leave
# model_max_length unset, which makes truncation=True alone a no-op and overruns the model's
# position embeddings with an "index out of range in self" crash instead of just truncating.
# split_to_token_limit keeps every chunk under this; the truncation on the tokenizer call is
# only the backstop for a single unsplittable run of text.
TRANSLATE_MAX_TOKENS = 512
# What a chunk is measured against before being split, leaving room for the ">>deu<<" target
# prefix and the special tokens that are added on top of the text itself.
TRANSLATE_TEXT_TOKENS = TRANSLATE_MAX_TOKENS - 16


def prepare_translation(
    tokenizer, model_id: str, text: str, source: str, target: str
) -> Tuple[Any, Dict[str, Any]]:
    """Tokenizer inputs and model.generate() kwargs for this model's language convention."""
    if model_family(model_id) == "prefix":
        text = f"{model_language_code(model_id, target)} {text}"
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=TRANSLATE_MAX_TOKENS)
    return inputs, {}


def language_codes():
    return sorted(CORE_LANGUAGES.values())


def normalize_translated_text(text: str) -> str:
    """OPUS-MT's detokenizer sometimes drops the space between a sentence-ending punctuation
    mark and the next sentence's capital letter (e.g. "teilen.Wenn" instead of "teilen. Wenn").
    Put it back; this only touches sentence-end-then-capital, so it never runs into ordinary
    mid-word text."""
    return re.sub(r"([.!?])(?=[A-ZÄÖÜ])", r"\1 ", text)


def token_count(tokenizer, text: str) -> int:
    try:
        return len(tokenizer(text).input_ids)
    except Exception:
        # Never let a measurement failure stop a translation; an over-long chunk is still
        # translated, just truncated as it was before.
        return 0


# Two alternatives, not one shared class: a Latin sentence needs the trailing whitespace to tell
# "Mr." or "3.14" from a real end, but CJK punctuation (。！？, U+3002/FF01/FF1F) carries no space
# after it at all - Japanese and Chinese prose runs terminator-to-first-character of the next
# sentence. Requiring \s+ for those matched nothing on a real Japanese page (verified: zero
# matches over a 3138-character story), so split_to_sentences fell through to
# split_to_token_limit's blind character-count bisection - the exact "long multi-sentence input"
# failure this whole function exists to avoid, just self-inflicted for every CJK document.
SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*\s+|[。！？][\"'”』】)\]]*")

# A period after one of these does not end the sentence - measured: "zzgl. MwSt. – Versand ist
# kostenlos." split at "MwSt." and came back "plus VAT. VAT – Shipping is free of charge.", the
# abbreviation translated twice because the splitter treated its period as a sentence end. Lower-
# case, without the trailing period. Not exhaustive or per-source-language (the source language of
# a chunk isn't known here) - covers the common German/English/French/Spanish cases; an
# abbreviation missing from this list just gets its ordinary period-then-space treatment back,
# same as before this list existed.
SENTENCE_END_ABBREVIATIONS = {
    # German
    "mwst", "zzgl", "inkl", "bzw", "ca", "etc", "usw", "z.b", "d.h", "u.a", "geb", "gest",
    "str", "nr", "tel", "dr", "prof", "hr", "fr", "sog", "ggf", "u.u", "z.zt", "evtl",
    # English
    "mr", "mrs", "ms", "st", "vs", "e.g", "i.e", "no", "vol", "fig", "approx", "dept",
    # French
    "mme", "mlle", "cf", "env",
    # Spanish
    "sr", "sra", "srta", "ud", "uds",
}
SENTENCE_END_LAST_WORD = re.compile(r"(\S+)$")


def ends_with_abbreviation(text_before_period: str) -> bool:
    match = SENTENCE_END_LAST_WORD.search(text_before_period)
    if not match:
        return False
    return match.group(1).strip(".,;:!?\"'()[]“”„»«").lower() in SENTENCE_END_ABBREVIATIONS


def split_to_sentences(tokenizer, text: str) -> List[str]:
    """Split text into one sentence per model call.

    OPUS-MT is trained on sentence pairs and quietly gives up part-way through a long multi-
    sentence input: measured on a real 1853-character paragraph (405 tokens, comfortably inside
    the 512-token window) it returned 59% of the text and stopped mid-list, dropping three
    bullet points. The same paragraph split into its 18 sentences came back complete. Nothing
    is lost in quality by splitting - the model carries no context across sentence boundaries
    anyway - and the sentences are translated in one batched call.
    """
    if not text.strip():
        return [text]
    # Cut *after* each sentence-ending match rather than splitting on it, so closing quotes and
    # brackets stay with their sentence instead of being eaten as part of the separator. A match
    # right after a known abbreviation is skipped - the word before the period decides, not the
    # match itself, since "MwSt." and "Freitag." use the exact same punctuation.
    sentences: List[str] = []
    start = 0
    for match in SENTENCE_END.finditer(text):
        if ends_with_abbreviation(text[:match.start()]):
            continue
        sentences.append(text[start:match.end()])
        start = match.end()
    if start < len(text):
        sentences.append(text[start:])
    sentences = [sentence for sentence in sentences if sentence.strip()]
    if len(sentences) > 1:
        return [part for sentence in sentences for part in split_to_token_limit(tokenizer, sentence)]
    return split_to_token_limit(tokenizer, text)


def split_to_token_limit(tokenizer, text: str) -> List[str]:
    """Split a single sentence into parts that fit the model's input window.

    `truncation=True` drops whatever does not fit *silently*. Sentences are normally far inside
    the window; this is the backstop for one enormous run of text, and for scripts where a
    character costs roughly a token.
    """
    if not text.strip() or token_count(tokenizer, text) <= TRANSLATE_TEXT_TOKENS:
        return [text]
    middle = len(text) // 2
    # Halve at the sentence end nearest the middle, falling back to a space and then to the
    # middle itself, so a part is never cut mid-word unless there is nothing else to cut at.
    split_at = 0
    for pattern in (r"[.!?…]['\")\]]?\s", r"\s"):
        positions = [match.end() for match in re.finditer(pattern, text)]
        inner = [position for position in positions if 0 < position < len(text)]
        if inner:
            split_at = min(inner, key=lambda position: abs(position - middle))
            break
    if not split_at:
        split_at = middle
    return (split_to_token_limit(tokenizer, text[:split_at])
            + split_to_token_limit(tokenizer, text[split_at:]))


# Dot leaders ("Kapitel eins . . . . . . 12") are decoration, not language. Handed to the model
# they read as an unfinished sentence, and it answers with whatever its training data pairs with
# a row of dots - for this model, several lines of European Parliament session boilerplate. They
# are dropped rather than put back afterwards: a leader is sized to the width the *original* line
# had left over, which the translation no longer has.
LEADER_RUN = re.compile(r"\s*(?:\.\s*){4,}")

# A rule to write an answer on, drawn as a run of underscores. Its own cell wherever it sits, see
# group_pdf_lines; four is well past anything a word carries and short of the shortest real one.
FORM_RULE = re.compile(r"_{4,}")

# The presentation forms of the Latin ligatures. A PDF that draws "Anschaffung" with an ff ligature
# extracts as "Anschaﬀung", which the model has never seen and which reaches the finished document
# unchanged. Expanding them is what NFKC would do, applied on its own so the rest of the text keeps
# its own normalisation.
PDF_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
                 "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}

# The same ligatures again, from producers whose ToUnicode table points them at Latin Extended-B
# instead: Stall-Kamera-System extracts "PosiƟon", "FestplaƩe", "SoŌware", and its translation then
# carried the mojibake through ("läuŌ" came back as "läuÅ").
#
# Only ever between letters of a word, because every one of these is a real capital in its own
# right: Ō carries the macron of Latin and of romanised Japanese, both languages this translates.
# A legitimate Ō opens a word or stands in capitals, so requiring a lowercase letter in front of it
# separates the two cleanly.
PDF_BROKEN_LIGATURES = {"Ɵ": "ti", "Ʃ": "tt", "Ō": "ft"}
PDF_BROKEN_LIGATURE_RUN = re.compile(
    r"(?<=[a-zà-öø-ÿ])[" + "".join(PDF_BROKEN_LIGATURES) + r"]")

# The lowercase half of the same damage: Stall-Kamera-System extracts "abrufbar" as "abruĩar".
# Nothing in the word tells this apart from the real letter the way a capital does - ĩ is an
# ordinary Vietnamese one - so it is only put back once the source language is known, and never
# for a language that writes it. Only the one mapping that was actually measured: which glyph a
# broken font points where is that font's own accident, and guessing further pairs would corrupt
# words no document has been seen to carry.
PDF_BROKEN_LOWERCASE_LIGATURES = {"ĩ": "fb"}
PDF_BROKEN_LOWERCASE_RUN = re.compile(
    r"(?<=[a-zà-öø-ÿ])[" + "".join(PDF_BROKEN_LOWERCASE_LIGATURES) + r"](?=[a-zà-öø-ÿ])")
# Languages that write these letters themselves, in the internal code's language part. Vietnamese
# was the only one and is no longer offered (see CORE_LANGUAGES), left empty rather than removed
# in case a future language needs the same carve-out.
LIGATURE_NATIVE_LANGUAGES: Set[str] = set()


def expand_pdf_ligatures(text: str) -> str:
    """Put ligature glyphs back into the letters they stand for, see PDF_LIGATURES."""
    for ligature, letters in PDF_LIGATURES.items():
        if ligature in text:
            text = text.replace(ligature, letters)
    return PDF_BROKEN_LIGATURE_RUN.sub(lambda hit: PDF_BROKEN_LIGATURES[hit.group()], text)


def expand_lowercase_ligatures(text: str, source: str) -> str:
    """Repair the lowercase mis-mapped ligatures, unless the source language writes them itself.

    Runs after extraction rather than inside it: with the source set to auto-detect there is no
    language to check against until the text has been read, see PDF_BROKEN_LOWERCASE_LIGATURES.
    """
    if source.split("_")[0] in LIGATURE_NATIVE_LANGUAGES:
        return text
    return PDF_BROKEN_LOWERCASE_RUN.sub(
        lambda hit: PDF_BROKEN_LOWERCASE_LIGATURES[hit.group()], text)

# A result this much longer than its source is not a translation. The fallback model answers short,
# low-content fragments - a page number, a list marker, a heading - by dumping training data
# ("Der Präsident. — Das Wort hat die Fraktion...", "== Weblinks =="), which the layout pipeline
# then lays out as if it were text and runs across the whole page. Measured against 153 real
# paragraph pairs from the test document: at these values the eleven invented ones are rejected and
# no genuine translation is, the longest of which grew from "My Story" to "Meine Geschichte" (x2.0,
# inside the margin). Applied per sentence, so the factor never has to cover a whole paragraph.
HALLUCINATION_LENGTH_FACTOR = 2.0
HALLUCINATION_LENGTH_MARGIN = 15

# The factor above counts characters, which only compares like with like as long as source and
# target write a word in roughly as many of them. Han and kana do not: they carry a whole word in
# one or two characters, so a faithful German translation of Japanese is several times its source
# in length and the factor threw it away. Hoshi_no_Kagi came back with every heading, the opening
# quote and three paragraphs left in Japanese for exactly this reason - not text lost in
# extraction, text the guard discarded after the model had translated it correctly.
#
# Measured (Aug 2026) against the fallback model, guard off, ja/zh -> de: genuine translations ran
# x1.70 to x6.25 of their source ("星の鍵と幻影の森" -> "Der Schlüssel zu den Sternen und der
# Schattenwald.", the tightest case at 8 characters in and 50 out). Weighting a Han or kana
# character as 2.5 ordinary ones passes all of them and still catches the training-data dumps,
# which run 100+ characters off a heading of ten.
#
# Hangul is deliberately *not* weighted. Measured the same way, ko -> de is not a translation
# problem the guard should relax for: the model answers Korean with Bible boilerplate ("Und es
# geschah, als der dritte Knabe diente...", x3.90, and one x34.30 degenerate loop). The guard is
# what keeps those off the page, so Korean keeps the unweighted budget.
CJK_LENGTH_WEIGHT = 2.5
# Hiragana and katakana, then the two Han blocks that carry ordinary text.
CJK_DENSE_CHARS = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


def weighted_length(text: str) -> float:
    """Length of `text` in units comparable across writing systems, see CJK_LENGTH_WEIGHT."""
    dense = len(CJK_DENSE_CHARS.findall(text))
    return len(text) + dense * (CJK_LENGTH_WEIGHT - 1)

# The other tell, for the ones that come back the same length as their source and so pass the check
# above: the model was trained on Wikipedia dumps and answers a standalone name or heading with the
# scaffolding of an article rather than a translation. "Russ Seigenberg, Ph.D." on the title page
# came back as "== Weblinks ==== Einzelnachweise ==" three times over. Section markers, links and
# templates; no translation produces them unless the source carried them too, which is checked.
HALLUCINATION_MARKUP = re.compile(r"==|\[\[|]]|\{\{|}}")

# Degenerate repetition ("ENTWICKLUNG DER ENTWICKLUNG DER ...", "iv iv iv iv") is the other half of
# the same failure, and beam search alone does not break out of it. Six *tokens* is more than one
# period of such a loop but still well above anything a single sentence repeats on purpose.
NO_REPEAT_NGRAM_SIZE = 6


def clean_source_text(text: str) -> str:
    return LEADER_RUN.sub(" ", text).strip()


def guard_hallucination(source: str, translated: str, target: str = "") -> str:
    """Keep the original wherever the model clearly invented rather than translated."""
    if len(translated) > HALLUCINATION_LENGTH_FACTOR * weighted_length(source) + HALLUCINATION_LENGTH_MARGIN:
        return source
    if HALLUCINATION_MARKUP.search(translated) and not HALLUCINATION_MARKUP.search(source):
        return source
    # A CJK character in a translation whose source carried none and whose target is not itself
    # written in one is invented, not translated - MatterhornProtokoll's footer, a short line
    # repeated on every page, came back "Competence Center 的 PDF/UA-1" on three of them, same
    # single character in the same spot each time. Under the length guard and free of
    # HALLUCINATION_MARKUP's wiki syntax, so nothing else catches it - and the one stray character
    # is enough to make detect_pdf_script pick the CJK fallback font for the entire line, which
    # does not cover Latin as well as the document's own font and garbled the rest of it too.
    if (CJK_DENSE_CHARS.search(translated) and not CJK_DENSE_CHARS.search(source)
            and not target.startswith(("zho", "jpn"))):
        return source
    return translated


def translate_one(text: str, source: str, target: str) -> str:
    text = clean_source_text(text)
    if not text:
        return ""

    model_id, _dedicated = resolve_model(source, target)
    begin_model_use()
    try:
        tokenizer, model, device, torch_module = load_model(model_id)
        results = []
        for part in split_to_sentences(tokenizer, text):
            inputs, generate_kwargs = prepare_translation(tokenizer, model_id, part, source, target)
            inputs = inputs.to(device)

            with torch_module.inference_mode():
                generated = model.generate(
                    **inputs,
                    **generate_kwargs,
                    max_new_tokens=TRANSLATE_MAX_TOKENS,
                    num_beams=4,
                    no_repeat_ngram_size=NO_REPEAT_NGRAM_SIZE,
                )
            decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)[0]
            # Guarded per sentence, because that is the unit the model invents in: one made-up
            # sentence in the middle of a paragraph used to take the whole paragraph down with it.
            results.append(guard_hallucination(part, decoded, target))
        joined = normalize_translated_text(" ".join(part.strip() for part in results if part.strip()))
        return guard_hallucination(text, joined, target)
    finally:
        end_model_use()


def translate_batch(texts: List[str], source: str, target: str) -> List[str]:
    """Translate several short texts in a single model.generate() call (real tensor batching,
    not string concatenation, so each result maps back to its input by position)."""
    texts = [clean_source_text(text) for text in texts]
    indices = [i for i, text in enumerate(texts) if text]
    if not indices:
        return ["" for _ in texts]

    model_id, _dedicated = resolve_model(source, target)
    begin_model_use()
    try:
        tokenizer, model, device, torch_module = load_model(model_id)
        # A text too long for the model's window is split first, and its parts are joined back
        # up afterwards, so batching never silently truncates one of its entries.
        batch_texts: List[str] = []
        owners: List[int] = []
        for i in indices:
            for part in split_to_sentences(tokenizer, texts[i]):
                batch_texts.append(part)
                owners.append(i)
        # Kept before the target-language prefix is glued on, so guard_hallucination below
        # measures against the sentence itself.
        sources = list(batch_texts)
        if model_family(model_id) == "prefix":
            prefix = model_language_code(model_id, target)
            batch_texts = [f"{prefix} {text}" for text in batch_texts]
        inputs = tokenizer(
            batch_texts, return_tensors="pt", truncation=True, max_length=TRANSLATE_MAX_TOKENS, padding=True
        ).to(device)

        with torch_module.inference_mode():
            generated = model.generate(
                **inputs,
                max_new_tokens=TRANSLATE_MAX_TOKENS,
                num_beams=4,
                no_repeat_ngram_size=NO_REPEAT_NGRAM_SIZE,
            )
        decoded = tokenizer.batch_decode(generated, skip_special_tokens=True)
    finally:
        end_model_use()

    parts: Dict[int, List[str]] = {}
    for owner, source_text, text in zip(owners, sources, decoded):
        text = guard_hallucination(source_text, text, target)
        if text.strip():
            parts.setdefault(owner, []).append(text.strip())
    results = ["" for _ in texts]
    for owner, pieces in parts.items():
        results[owner] = guard_hallucination(texts[owner], normalize_translated_text(" ".join(pieces)), target)
    return results


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


# Base-14 stand-ins, used when no file for the face could be loaded.
PDF_BASE_FONTS = {(False, False): "helv", (True, False): "hebo",
                  (False, True): "tiro", (True, True): "tibo"}


@lru_cache(maxsize=32)
def font_file(bold: bool = False, script: str = "", serif: bool = False,
              italic: bool = False) -> str:
    """The font file a face is drawn and measured with, or "" for the base-14 stand-in.

    A base-14 font can only show WinAnsi characters, so any non-Latin target language (Cyrillic,
    Greek, ...) would come out as garbage. `script` (from detect_pdf_script) picks a file that
    actually covers CJK/Arabic/Devanagari/Hebrew instead, where DejaVu Sans has no glyphs at all;
    those fonts are used as-is for "bold" and for italic too, since covering the script matters
    more than the weight or the slant.

    An italic face falls back to the upright file rather than to the base-14 stand-in when no
    italic file is installed: losing the slant costs a document its emphasis, while dropping to
    base-14 would cost a Cyrillic or Greek one its glyphs.
    """
    if script and script in PDF_SCRIPT_FONT_CANDIDATES:
        candidates = PDF_SCRIPT_FONT_CANDIDATES[script]
    elif serif and italic:
        candidates = PDF_SERIF_BOLD_ITALIC_CANDIDATES if bold else PDF_SERIF_ITALIC_CANDIDATES
    elif serif:
        candidates = PDF_SERIF_BOLD_CANDIDATES if bold else PDF_SERIF_CANDIDATES
    elif italic:
        candidates = PDF_FONT_BOLD_ITALIC_CANDIDATES if bold else PDF_FONT_ITALIC_CANDIDATES
    else:
        candidates = PDF_FONT_BOLD_CANDIDATES if bold else PDF_FONT_CANDIDATES
    for path in candidates:
        if path and Path(path).exists():
            return path
    return font_file(bold, script, serif) if italic else ""


@lru_cache(maxsize=32)
def script_font(bold: bool = False, script: str = "", serif: bool = False, italic: bool = False):
    """The whole font as MuPDF sees it, for measuring. Cached because MuPDF parses the file again
    for every font object, and a document is measured line by line."""
    path = font_file(bold, script, serif, italic)
    if path:
        try:
            return pymupdf.Font(fontfile=path)
        except Exception:
            pass
    return pymupdf.Font(PDF_BASE_FONTS[(bold, serif)])


def subset_font(bold: bool, script: str, serif: bool, italic: bool, codepoints: Set[int]):
    """The same font cut down to the characters the document actually draws.

    MuPDF embeds whatever font it is handed whole, and its own `subset_fonts` cannot cut the
    CFF-based Noto CJK collection back down again ("format error: Index bounds"), which left a
    single page of Japanese weighing 13.7 MB. Subsetting before drawing works for every font
    format and brought that page to 6 KB.
    """
    path = font_file(bold, script, serif, italic)
    if path:
        try:
            import logging

            from fontTools import subset
            from fontTools.ttLib import TTFont

            # "meta NOT subset; don't know how to subset; dropped" on every font, every document.
            logging.getLogger("fontTools.subset").setLevel(logging.ERROR)
            ttf = TTFont(path, fontNumber=0, lazy=True)
            subsetter = subset.Subsetter(options=subset.Options(notdef_outline=True))
            subsetter.populate(unicodes=sorted(codepoints))
            subsetter.subset(ttf)
            buffer = BytesIO()
            ttf.save(buffer)
            return pymupdf.Font(fontbuffer=buffer.getvalue())
        except Exception:
            pass
    return script_font(bold, script, serif, italic)


def pdf_measure_text(text: str, size: float, bold: bool = False, serif: bool = False,
                     italic: bool = False) -> float:
    return script_font(bold, detect_pdf_script(text), serif, italic).text_length(text, fontsize=size)


def wrap_pdf_line(text: str, size: float = PDF_FONT_SIZE) -> List[str]:
    # Measured by actual glyph width (script-aware, see detect_pdf_script), not a fixed character
    # count: CJK glyphs run close to twice as wide as Latin ones at the same point size, so a
    # char-count cap tuned for Latin text ran CJK lines off the page edge.
    if not text:
        return [""]
    return wrap_text_to_width(text, PDF_PAGE_WIDTH - 2 * PDF_MARGIN, size)


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
                for line in wrap_pdf_line(heading, PDF_HEADING_FONT_SIZE):
                    lines.append({"text": line, "font": "F2", "size": PDF_HEADING_FONT_SIZE, "line_height": 18})
                lines.append({"text": "", "font": "F1", "size": PDF_FONT_SIZE, "line_height": 8})
                continue
        wrapped = wrap_pdf_line(stripped)
        for line in wrapped:
            lines.append({"text": line, "font": "F1", "size": PDF_FONT_SIZE, "line_height": PDF_LINE_HEIGHT})
    return lines


def paginate_pdf_lines(lines: List[Dict[str, Any]],
                       usable_height: float = PDF_PAGE_HEIGHT - (2 * PDF_MARGIN) - 26
                       ) -> List[List[Dict[str, Any]]]:
    pages = [[]]
    used_height = 0
    for line in lines:
        line_height = line["line_height"]
        if pages[-1] and used_height + line_height > usable_height:
            pages.append([])
            used_height = 0
        pages[-1].append(line)
        used_height += line_height
    return pages or [[]]


def pdf_image_blocks_width(image: Dict[str, float], width: float, margin: float) -> bool:
    """Whether `image` leaves no side gap worth wrapping a line of text into
    (PDF_IMAGE_MIN_TEXT_BAND). Such an image (a full-page scan, typically) is dropped from the
    re-exported translation entirely rather than reflowed around - see pdf_document_pages -
    leaving the plain, image-less text export instead of pushing every line onto a page of its
    own below it."""
    occupied = min(image["right"], margin + width) - max(image["x"], margin)
    return width - occupied < PDF_IMAGE_MIN_TEXT_BAND


def image_line_bands(images: List[Dict[str, float]], width: float, margin: float,
                     top: float, line_height: float) -> List[Tuple[float, float]]:
    """The (x, usable width) a line of text may occupy at each step down from `top`, narrowed by
    whichever of `images` reach that height, widest free gap first. `images` is assumed already
    filtered of anything pdf_image_blocks_width calls wide (see pdf_document_pages) - what is left
    here is genuinely partial-width images, where "too narrow a gap" only means several of them
    jointly leaving less than PDF_IMAGE_MIN_TEXT_BAND free, not one image covering the whole line.

    Stops once past the lowest image and appends one trailing full-width entry: wrap_text_to_width
    reuses a width list's last entry for every line past its length, so the caller never has to
    know in advance how many lines a paragraph will take.
    """
    if not images:
        return [(margin, width)]
    lowest = min(image["bottom"] for image in images)
    bands = []
    y = top
    while y > lowest:
        covers = sorted((max(image["x"], margin), min(image["right"], margin + width))
                        for image in images if image["bottom"] <= y <= image["top"])
        gaps = []
        cursor = margin
        for left, right in covers:
            if left > cursor:
                gaps.append((cursor, left - cursor))
            cursor = max(cursor, right)
        if cursor < margin + width:
            gaps.append((cursor, margin + width - cursor))
        x, usable = max(gaps, key=lambda gap: gap[1]) if gaps else (margin, 0.0)
        if usable < PDF_IMAGE_MIN_TEXT_BAND:
            # Several individually-narrow images jointly leaving no real gap on this one line -
            # full width, text overlaps them here rather than the page failing to render.
            x, usable = margin, width
        bands.append((x, usable))
        y -= line_height
    bands.append((margin, width))
    return bands


def pdf_render_lines_with_images(text: str, images: List[Dict[str, float]], width: float,
                                 margin: float, top: float) -> List[Dict[str, Any]]:
    """pdf_render_lines, narrowed around `images` active at each line's height (image_line_bands).

    Only used for the first output page of a source page that actually has images - continuation
    pages, and every source page without one, keep the plain fixed-width pdf_render_lines. A '#'
    heading inside a page's own body text is not something OCR or plain PDF extraction produces
    (it only ever prefixes the page as a whole, added separately by create_pdf_from_pages), so it
    is drawn at full width rather than teaching the band logic a second font's line height too.
    """
    lines = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    y = top
    for paragraph in normalized.split("\n"):
        stripped = paragraph.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                for line in wrap_pdf_line(heading, PDF_HEADING_FONT_SIZE):
                    lines.append({"text": line, "font": "F2", "size": PDF_HEADING_FONT_SIZE,
                                 "line_height": 18, "x": margin})
                    y -= 18
                lines.append({"text": "", "font": "F1", "size": PDF_FONT_SIZE, "line_height": 8,
                             "x": margin})
                y -= 8
                continue
        bands = image_line_bands(images, width, margin, y, PDF_LINE_HEIGHT)
        wrapped = wrap_text_to_width(stripped, [band[1] for band in bands], PDF_FONT_SIZE)
        for index, line in enumerate(wrapped):
            lines.append({"text": line, "font": "F1", "size": PDF_FONT_SIZE,
                         "line_height": PDF_LINE_HEIGHT,
                         "x": bands[min(index, len(bands) - 1)][0]})
            y -= PDF_LINE_HEIGHT
    return lines


def pdf_document_pages(text: str,
                       page_images: Optional[Dict[str, Dict[str, Any]]] = None
                       ) -> List[Dict[str, Any]]:
    page_images = page_images or {}
    document_pages = []
    for section in markdown_page_sections(text):
        info = page_images.get(section["page_number"])
        images = []
        if info and info["images"]:
            # An image wide enough to leave no usable side gap (a full-page scan, typically)
            # is dropped rather than reflowed around: pushing every line of text below it would
            # turn one source page into two output pages, translation on its own page with
            # nothing to show for the original - worse than just not drawing that image at all.
            images = [image for image in info["images"]
                     if not pdf_image_blocks_width(image, info["width"] - 2 * PDF_MARGIN, PDF_MARGIN)]
        if images:
            width, height = info["width"], info["height"]
            margin = PDF_MARGIN
            # Below the "Page N" heading and its 24pt gap, exactly where create_pdf_from_pages
            # starts drawing page_data["lines"] - see its own y = height - margin, y -= 24.
            lines = pdf_render_lines_with_images(
                section["text"], images, width - 2 * margin, margin, height - margin - 24)
            content_pages = paginate_pdf_lines(lines, height - 2 * margin - 26)
            for index, page_lines in enumerate(content_pages, start=1):
                page_dict = {
                    "source_page": section["page_number"],
                    "continuation": index > 1,
                    "lines": page_lines,
                    "width": width,
                    "height": height,
                }
                if index == 1:
                    # Never repeated on a continuation page: the image already sat once at its
                    # real position, and there is no second "real position" to put it at.
                    page_dict["images"] = images
                document_pages.append(page_dict)
            continue
        content_pages = paginate_pdf_lines(pdf_render_lines(section["text"]))
        for index, lines in enumerate(content_pages, start=1):
            document_pages.append({
                "source_page": section["page_number"],
                "continuation": index > 1,
                "lines": lines,
            })
    return document_pages or [{"source_page": "", "continuation": False, "lines": []}]


def pdf_font_key(text: str, face: str) -> Tuple[bool, str, bool, bool]:
    """Which font one line is drawn with: (bold, script, serif, italic)."""
    bold, serif, italic = PDF_FONT_FACES.get(face, (False, False, False))
    return bold, detect_pdf_script(text), serif, italic


def draw_pdf_line(page, text: str, x: float, y: float, face: str,
                  size: float, color: int, height: float,
                  justify_to: Optional[float], fonts: Dict) -> None:
    """One line of text on a MuPDF page.

    `y` is a PDF baseline, counted up from the bottom of the page; MuPDF counts down from the
    top, hence the flip.

    Written through a TextWriter rather than insert_text for the one thing insert_text has no
    switch for: a Hebrew or Arabic line has to be laid down right to left, or it ends up on the
    page back to front. Everything else comes out of both the same, glyph for glyph.
    """
    font = fonts[pdf_font_key(text, face)]
    writer = pymupdf.TextWriter(page.rect)
    if is_rtl_text(text):
        # MuPDF's own right_to_left turns the whole string around, a year or a Latin name inside
        # the line with it. So the line is handed over a stretch at a time, laid out from its
        # right edge leftwards, each stretch written in the direction it actually runs.
        cursor = x
        space = font.text_length(" ", fontsize=size)
        for part, rtl in reversed(direction_segments(text)):
            part = part.strip()
            if not part:
                continue
            writer.append((cursor, height - y), part, font=font, fontsize=size,
                          right_to_left=rtl)
            cursor += font.text_length(part, fontsize=size) + space
    elif justify_to and " " in text.strip():
        # Justified: the words are placed one by one with the leftover width shared out between
        # them. Only their spacing changes, never the line's own place on the page.
        words = [word for word in text.split(" ") if word]
        ink = sum(font.text_length(word, fontsize=size) for word in words)
        gap = (justify_to - x - ink) / (len(words) - 1) if len(words) > 1 else 0.0
        space = font.text_length(" ", fontsize=size)
        if not space < gap <= PDF_JUSTIFY_MAX_SPACE * space:
            # Already full, or so short that filling it would tear the line apart.
            writer.append((x, height - y), text, font=font, fontsize=size)
        else:
            cursor = x
            for word in words:
                writer.append((cursor, height - y), word, font=font, fontsize=size)
                cursor += font.text_length(word, fontsize=size) + gap
    else:
        writer.append((x, height - y), text, font=font, fontsize=size)
    writer.write_text(
        page,
        color=((color >> 16 & 0xFF) / 255, (color >> 8 & 0xFF) / 255, (color & 0xFF) / 255),
    )


def fix_pdf_space_mapping(document) -> None:
    """Map the space glyph back to U+0020 in every ToUnicode table MuPDF wrote.

    A font's cmap points both space and no-break space at the same glyph, and MuPDF builds its
    ToUnicode by reversing that map, so the glyph comes back as U+00A0. Copy/paste and Ctrl+F in
    the finished document then miss every phrase longer than one word.
    """
    for xref in range(1, document.xref_length()):
        if not document.xref_is_stream(xref):
            continue
        stream = document.xref_stream(xref)
        if b"beginbfchar" not in stream:
            continue
        # Only the target of a single-glyph mapping, never the glyph id in front of it.
        patched = re.sub(rb"(<[0-9a-fA-F]{4}> )<00[aA]0>", rb"\g<1><0020>", stream)
        if patched != stream:
            document.update_stream(xref, patched)


def create_pdf_from_pages(pages: List[Dict[str, Any]]) -> bytes:
    """Write the pages with MuPDF, which shapes complex scripts and embeds the fonts itself.

    The lines are collected before any of them is drawn, so that each font can be subset to the
    characters this document actually uses (see subset_font) before it is handed to MuPDF.
    """
    with pymupdf.open() as document:
        # (page index, text, x, y, face, size, color, page height, edge to justify to). The index
        # rather than the page:
        # adding a page invalidates the page objects handed out before it.
        lines: List[Tuple[Any, ...]] = []
        # (page index, image dict) - drawn in its own pass after every page exists, same reason.
        images: List[Tuple[int, Dict[str, Any]]] = []
        for output_page_number, page_data in enumerate(pages, start=1):
            width = page_data.get("width", PDF_PAGE_WIDTH)
            height = page_data.get("height", PDF_PAGE_HEIGHT)
            margin = page_data.get("margin", PDF_MARGIN)
            document.new_page(width=width, height=height)
            index = output_page_number - 1
            for image in page_data.get("images") or []:
                images.append((index, image, height))
            y = height - margin
            if page_data["source_page"]:
                heading = "Page " + page_data["source_page"]
                if page_data["continuation"]:
                    heading += " continued"
                lines.append((index, heading, margin, y, "F2", PDF_HEADING_FONT_SIZE, 0, height, None))
                y -= 24
            for line in page_data["lines"]:
                if line["text"]:
                    lines.append((index, line["text"], line.get("x", margin), line.get("y", y),
                                  line["font"], line["size"], line.get("color", 0), height,
                                  line.get("justify_to")))
                y -= line["line_height"]
            if page_data.get("footer", True):
                lines.append((index, f"{output_page_number}", width - margin, margin // 2,
                              "F1", PDF_FOOTER_FONT_SIZE, 0, height, None))

        codepoints: Dict[Tuple[bool, str, bool, bool], Set[int]] = {}
        for _, text, _, _, face, _, _, _, _ in lines:
            codepoints.setdefault(pdf_font_key(text, face), set()).update(map(ord, text))
        fonts = {key: subset_font(*key, points) for key, points in codepoints.items()}
        for index, image, height in images:
            # y-up (bottom counted from the page's own bottom, like every other PDF coordinate this
            # module works in), flipped to MuPDF's y-down insert_image space here - the same flip
            # draw_pdf_line does for text.
            rect = pymupdf.Rect(image["x"], height - image["top"],
                                image["right"], height - image["bottom"])
            try:
                document[index].insert_image(rect, stream=image["bytes"])
            except Exception:
                pass
        for index, *line in lines:
            draw_pdf_line(document[index], *line, fonts)

        # Second pass over MuPDF's own doing: for a character none of our fonts covers it silently
        # falls back to a built-in face and embeds that one whole (3.5 MB of Droid Sans Fallback for
        # one Korean word). subset_fonts cuts those down; it is no help with the fonts we picked
        # ourselves, see subset_font.
        try:
            document.subset_fonts(verbose=False)
        except Exception:
            # A fat PDF is still a readable one.
            pass
        fix_pdf_space_mapping(document)
        return document.tobytes(garbage=3, deflate=True)


def create_text_pdf(text: str, source_content: bytes = b"") -> bytes:
    """A fresh PDF from plain translated text (see create_pdf_from_pages), with the original's
    own images placed back at their original position when `source_content` is the source PDF
    itself - see extract_pdf_page_images. Every other caller has no PDF to take images from
    (arbitrary text, or a translation whose source was never a PDF) and gets the plain export
    unchanged."""
    page_images = extract_pdf_page_images(source_content) if source_content else None
    return create_pdf_from_pages(pdf_document_pages(text, page_images))


def docx_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def create_text_docx(text: str) -> bytes:
    """Build a minimal but valid DOCX from plain translated text, for sources (PDF, plain text)
    that have no original DOCX structure to preserve. Mirrors create_text_pdf's markdown
    handling: a '#'-prefixed line renders as a bold heading, everything else is its own
    paragraph (Word wraps long lines itself, unlike the hand-built PDF path)."""
    paragraphs = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        stripped = line.strip()
        if not stripped:
            paragraphs.append("<w:p/>")
        elif stripped.startswith("#"):
            heading = docx_escape(stripped.lstrip("#").strip())
            paragraphs.append(
                '<w:p><w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>'
                f'<w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t xml:space="preserve">{heading}</w:t></w:r></w:p>'
            )
        else:
            paragraphs.append(f'<w:p><w:r><w:t xml:space="preserve">{docx_escape(stripped)}</w:t></w:r></w:p>')
    body = "".join(paragraphs) or "<w:p/>"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types)
        docx.writestr("_rels/.rels", package_rels)
        docx.writestr("word/document.xml", document)
    return output.getvalue()


def cleanup_history():
    """Delete history entries past their retention time, whole.

    Per entry, not per file: an entry is a metadata file plus the translation, the retained source
    and, once someone downloads it, the prepared original-format export. That export is written
    later than the rest, so going by each file's own age would have left the translated document
    on disk after everything else about it was gone.

    Files whose metadata is already missing are still dropped by their own age, which is how a
    half-deleted entry from an earlier pass finally disappears.
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - (HISTORY_HOURS * 3600)
    expired = {path.name[:-len(".json")] for path in HISTORY_DIR.glob("*.json")
               if path.stat().st_mtime < cutoff}
    # ponytail: entries x files per pass, fine at the few hundred a retention window holds.
    for path in HISTORY_DIR.iterdir():
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
    return HISTORY_DIR / f"{item_id}.source.{safe_extension}"


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
    return HISTORY_DIR / f"{item_id}.export.{safe_extension}"


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
        return moment.astimezone(ZoneInfo(HISTORY_TIMEZONE))
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
    for meta_path in HISTORY_DIR.glob("*.json"):
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
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return items


def history_paths(item_id: str):
    if ".." in item_id or "/" in item_id or "\\" in item_id:
        raise HTTPException(status_code=400, detail="Invalid history id")
    return HISTORY_DIR / f"{item_id}.md", HISTORY_DIR / f"{item_id}.json"


def history_item(item_id: str, required: bool = True) -> Dict[str, Any]:
    _, json_path = history_paths(item_id)
    if not json_path.exists():
        if not required:
            # cleanup_history deletes files one by one, so the metadata can already be gone while
            # the result is still there. A download then falls back to a generic name, not a 404.
            return {}
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


# How long a finished job stays in the queue before it is dropped. Its translation is in the
# history by then; what the job itself still answers for is the tab that was watching it, and an
# hour is far longer than anyone waits for that.
FINISHED_JOB_RETENTION_SECONDS = 3600


def cleanup_finished_jobs():
    """Drop jobs that ended long ago, from memory and from disk.

    Nothing ever removed them: 137 of them had piled up on the test machine, all complete, held in
    memory, written out as one file each, reloaded on every restart and sent over the wire with
    every poll. Only the ones still queued or running have anything left to do.
    """
    cutoff = time.time() - FINISHED_JOB_RETENTION_SECONDS
    with JOBS_LOCK:
        stale = [job_id for job_id, job in JOBS.items()
                 if job.get("status") in ("complete", "failed", "cancelled")
                 # Falls back to when it was queued: a record without a finish time is not a
                 # reason to keep it for ever, but it should not vanish the moment it appears.
                 and (job.get("finished_at") or job.get("queued_at") or 0) < cutoff]
        for job_id in stale:
            JOBS.pop(job_id, None)
            JOB_RUNNERS.pop(job_id, None)
            JOB_LAST_PERSISTED_AT.pop(job_id, None)
            job_json_path(job_id).unlink(missing_ok=True)


def list_jobs():
    """The queue as the UI polls it, every few seconds, without the translated text.

    A finished job keeps its whole result in memory for /jobs/{id} to hand back, and carrying that
    through the listing made the answer 136 KB where the list shows none of it - three seconds
    later, and again three seconds after that, for every open tab.
    """
    cleanup_finished_jobs()
    with JOBS_LOCK:
        positions = queue_positions_by_job_id()
        jobs = [public_job(job) for job in sorted(JOBS.values(), key=job_sort_key)]
        for job in jobs:
            job["position"] = positions.get(job["id"])
            job.pop("result", None)
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
            percent = (current / total) * 100
            if job.get("status") == "running" and current >= total:
                # Every chunk translated, but a PDF job still has to render the result -
                # redaction, reflow, font subsetting - which can take real time on a big
                # document. Without the cap the bar and the number both read as finished long
                # before the file is actually there to download.
                percent = min(percent, 99.0)
            job["percent"] = round(percent, 1)
        if started_at and current and total and current < total:
            elapsed = time.time() - started_at
            job["eta_seconds"] = round((elapsed / current) * (total - current))
        elif current and total and current >= total:
            job["eta_seconds"] = 0
        now = time.time()
        terminal = job.get("status") in ("complete", "failed", "cancelled")
        control_flag_changed = "cancel_requested" in values or "pause_requested" in values
        if terminal or control_flag_changed or now - JOB_LAST_PERSISTED_AT.get(job_id, 0) >= JOB_PERSIST_MIN_INTERVAL_SECONDS:
            persist_job(job)
            JOB_LAST_PERSISTED_AT[job_id] = now
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
    if action not in ("pause", "resume", "cancel"):
        raise HTTPException(status_code=400, detail="Unknown job action")
    current = get_job(job_id)
    if action == "pause":
        update_job(job_id, pause_requested=True, message="Pause requested")
    elif action == "resume":
        update_job(job_id, pause_requested=False, message="Resuming")
    elif action == "cancel":
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
            job.get("page_range", ""),
        )
    # An unrecognised kind (a hand-edited or corrupted job file, or one from a kind since
    # removed) must not come back as None with the job left "queued" - job_worker_loop would
    # just pick the same job again with nothing to make it wait, spinning one worker at 100% CPU
    # forever instead of blocking on JOBS_CONDITION.wait() like every other empty queue does.
    update_job(job_id, status="failed", message="Failed", error=f"Unknown job kind: {job.get('kind')}", finished_at=time.time())
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


def translate_chunks_batched(chunks: List[str], source: str, target: str, job_id: str, batch_size: int) -> List[str]:
    translated: List[str] = []
    for start in range(0, len(chunks), batch_size):
        wait_if_paused_or_cancelled(job_id)
        group = chunks[start:start + batch_size]
        update_job(job_id, status="running", current=start, message=f"Translating {start} / {len(chunks)} chunks")
        translated.extend(translate_batch(group, source, target))
        update_job(job_id, current=start + len(group))
    return translated


MARKDOWN_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)?)(.*)$")


def split_markdown_blocks(markdown: str) -> List[Tuple[str, str]]:
    """Split each line into its Markdown marker and the text after it.

    The marker has to stay out of the translation: given "## Zweiter Abschnitt" the model
    happily returns a sentence without the "##", and the heading arrives in the finished
    document as ordinary body text. Same for list bullets and quote marks. A fenced code block
    is kept out entirely - the model has no notion of "this is code", so a variable name inside
    the fence gets "translated" like prose (measured: `code_bleibt_unveraendert()` came back
    `code_remains_unchanged()`) - by folding each of its lines whole into the marker slot, the
    same way a marker line already skips translation for having no word in it.
    """
    blocks: List[Tuple[str, str]] = []
    in_code_fence = False
    for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip().startswith("```") or line.strip().startswith("~~~"):
            in_code_fence = not in_code_fence
            blocks.append(("", line))
            continue
        if in_code_fence:
            blocks.append((line, ""))
            continue
        match = MARKDOWN_PREFIX.match(line)
        blocks.append((match.group(1), match.group(2)) if match else ("", line))
    return blocks


def translate_markdown_document(markdown: str, source: str, target: str, job_id: str) -> Tuple[str, int]:
    blocks = split_markdown_blocks(markdown)
    # Only lines with something to translate are sent; the markers, blank lines and any
    # wordless leftovers (fenced code included) keep their place so the document reassembles
    # line for line.
    translatable = [index for index, (_prefix, text) in enumerate(blocks) if has_translatable_text(text)]
    chunks = [blocks[index][1] for index in translatable]
    update_job(job_id, total=len(chunks), message=f"Translating 0 / {len(chunks)} chunks")
    translated_chunks = translate_chunks_batched(chunks, source, target, job_id, PDF_LAYOUT_BATCH_SIZE)

    lines = [prefix + text for prefix, text in blocks]
    for index, translated in zip(translatable, translated_chunks):
        prefix, _original = blocks[index]
        translated = re.sub(r"\s+", " ", translated).strip()
        # The model treats "](url)" as ordinary prose and habitually adds the space it would put
        # before any other parenthetical, splitting "[Link](url)" into "[Link] (url)" - not a
        # link in any Markdown renderer. Only translated text passes through here, so this can't
        # touch a source line that was never sent to the model.
        translated = re.sub(r"\]\s+\(", "](", translated)
        lines[index] = prefix + translated
    return "\n".join(lines).strip(), len(chunks)


def parse_page_range(page_range: str, total_pages: int) -> List[int]:
    if not page_range.strip():
        return list(range(1, total_pages + 1))

    pages = set()
    # A range running past the last page is clamped ("the first 5 pages" is a reasonable thing to
    # ask of a 2-page document), but a single page named outright is not: "7" on a 3-page document
    # is a typo worth reporting rather than silently dropping.
    named_pages = set()
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
                pages.update(range(start, min(end, total_pages) + 1))
            else:
                page = int(part)
                pages.add(page)
                named_pages.add(page)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid page range") from exc

    selected = sorted(page for page in pages if page <= total_pages)
    invalid = named_pages - set(selected)
    if not selected or selected[0] < 1 or invalid:
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


OCR_FALLBACK_LANGUAGE = "eng"
# Tesseract names a few languages differently than we do, same reason as FALLBACK_LANGUAGE_ALIASES.
OCR_LANGUAGE_ALIASES = {"zho": "chi_sim"}


@lru_cache(maxsize=1)
def installed_ocr_languages() -> Tuple[str, ...]:
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"], check=True, capture_output=True, text=True
        )
    except Exception:
        return ()
    # The list is preceded by a header line ("List of available languages in ..."); language
    # codes never contain a space, which sorts it out without counting on it being line one.
    return tuple(line.strip() for line in result.stdout.splitlines()
                 if line.strip() and " " not in line.strip())


def tesseract_code(source: str) -> str:
    """Tesseract's name for an internal language code, whether or not it is installed."""
    base = source.split("_")[0]
    return OCR_LANGUAGE_ALIASES.get(base, base)


def ocr_language_code(source: str) -> str:
    """Tesseract language for a job's source language, or English when we cannot serve it."""
    if source == AUTO_SOURCE:
        return OCR_FALLBACK_LANGUAGE
    code = tesseract_code(source)
    installed = installed_ocr_languages()
    # An unknown -l aborts tesseract and with it the whole job, which is worse than reading one
    # page in the wrong language. Empty list means we could not ask, then just try the code.
    if installed and code not in installed:
        return OCR_FALLBACK_LANGUAGE
    return code


# What OSD calls a script, and the suffix CORE_LANGUAGES uses for it.
OCR_SCRIPT_SUFFIXES = {
    "Latin": "Latn", "Cyrillic": "Cyrl", "Arabic": "Arab", "Greek": "Grek",
    "Hebrew": "Hebr", "Devanagari": "Deva", "Han": "Hans", "HanS": "Hans", "HanT": "Hans",
    "Japanese": "Jpan", "Hiragana": "Jpan", "Katakana": "Jpan",
}
# Traditional Chinese has no entry in CORE_LANGUAGES - we translate to and from simplified - but
# reading a traditional scan with the simplified model rewrites characters and gets some wrong
# (生成器 became 生成锅). Where OSD names the traditional variant, read it with its own model.
OCR_SCRIPT_LANGUAGES = {"HanT": ("chi_tra",)}
# Each further language in one -l makes tesseract less accurate, so the probe stays short:
# coverage comes from the script OSD read off the image, not from a longer list.
OCR_PROBE_LIMIT = 3
OSD_SCRIPT = re.compile(r"^Script:\s*(\S+)", re.MULTILINE)
OSD_SCRIPT_CONFIDENCE = re.compile(r"^Script confidence:\s*([\d.]+)", re.MULTILINE)
# A guess below this is noise, not a reading. Measured 14.08.2026 on real pages: a numbers-heavy
# German table page misread as Cyrillic scored 0.42 and 1.67, while pages OSD was actually right
# to flag - a genuinely Hebrew page whose text layer claims Latin - scored 2.50 and 12.22.
# scripts_are_compatible below this is worth as little as no answer at all.
OCR_SCRIPT_MIN_CONFIDENCE = 2.0
# Levels in tesseract's TSV output: 2 is a block of text, 5 a single word.
TSV_BLOCK_LEVEL = 2
TSV_WORD_LEVEL = 5
# How much text a block has to hold before its own OSD reading is trusted over the page's. See
# ocr_block_scripts for the measurement behind it.
OCR_BLOCK_MIN_CHARS = 100


def run_tesseract(image_path: Path, languages: str) -> str:
    result = subprocess.run(
        ["tesseract", str(image_path), "stdout", "-l", languages],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def ocr_page_osd(image_path: Path) -> Tuple[str, float]:
    """The script OSD sees in a page image and its confidence, ("", 0.0) when it cannot tell."""
    try:
        result = subprocess.run(
            # OSD refuses below 50 characters by default and exits with an error. A scanned page
            # holding one short line is exactly the case that needs it most: measured on a
            # Japanese book, two of three pages got no answer at all, fell back to English and
            # came back empty, because English reads nothing off Japanese script.
            ["tesseract", str(image_path), "stdout", "--psm", "0",
             "-c", "min_characters_to_try=10"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        # Too little text even for that, no osd traineddata, a failed call: none of it may end
        # the job. A wrong script only costs the throwaway probe, which the recheck can correct.
        return "", 0.0
    script_match = OSD_SCRIPT.search(result.stdout)
    confidence_match = OSD_SCRIPT_CONFIDENCE.search(result.stdout)
    script = script_match.group(1) if script_match else ""
    confidence = float(confidence_match.group(1)) if confidence_match else 0.0
    return script, confidence


def ocr_page_script(image_path: Path) -> str:
    """The script OSD sees in a page image ("Latin", "Cyrillic", …), empty when it cannot tell."""
    return ocr_page_osd(image_path)[0]


def ocr_script_is_usable(script: str) -> bool:
    """Whether a language can be picked for this script at all.

    ocr_probe_languages answers English for anything it does not know, so an unusable script is
    worse than no script: it reads Han or Devanagari as English, which returns nothing.
    """
    return bool(script) and (script in OCR_SCRIPT_SUFFIXES or script in OCR_SCRIPT_LANGUAGES)


def scripts_are_compatible(first: str, second: str) -> bool:
    """Whether two OSD script names describe writing that is read with the same language."""
    normalise = lambda name: "Han" if name in ("HanS", "HanT") else name  # noqa: E731
    first, second = normalise(first), normalise(second)
    return first == second or frozenset((first, second)) in COMPATIBLE_SCRIPTS


def ocr_probe_languages(script: str) -> str:
    """Languages for the probe run, as one -l argument. Order follows CORE_LANGUAGES."""
    suffix = OCR_SCRIPT_SUFFIXES.get(script)
    installed = installed_ocr_languages()
    codes = [code for code in OCR_SCRIPT_LANGUAGES.get(script, ())
             if not installed or code in installed]
    if suffix and len(codes) < OCR_PROBE_LIMIT:
        for internal in CORE_LANGUAGES.values():
            if not internal.endswith(suffix):
                continue
            # tesseract_code, not ocr_language_code: the latter answers "eng" for a language
            # whose package is missing, which would drop English into a Cyrillic probe.
            code = tesseract_code(internal)
            if code not in codes and (not installed or code in installed):
                codes.append(code)
            if len(codes) >= OCR_PROBE_LIMIT:
                break
    return "+".join(codes) if codes else OCR_FALLBACK_LANGUAGE


# pdftoppm's own default, but stated rather than assumed: the block boxes tesseract reports are
# in pixels of this rendering, and render_pdf_region has to map them back to PDF points.
OCR_DPI = 150


def render_pdf_page(content: bytes, page_number: int, temp_dir: str) -> Path:
    """One page of a PDF as a PNG, the form both OSD and tesseract want."""
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
            "-r",
            str(OCR_DPI),
            "-png",
            "-singlefile",
            str(pdf_path),
            str(output_prefix),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return output_prefix.with_suffix(".png")


# A block is re-rendered slightly larger than tesseract measured it: its box hugs the ink, and
# characters right against the edge of an image read worse than ones with a margin around them.
OCR_REGION_PADDING = 8


def render_pdf_region(content: bytes, page_number: int, box: Tuple[int, int, int, int],
                      temp_dir: str, name: str) -> Path:
    """One block of a page as its own PNG, `box` in pixels of the OCR_DPI page rendering.

    Re-rendered from the PDF rather than cut out of the page image: PyMuPDF has no working
    pixmap-crop constructor left, and rasterising the region directly is both sharper and one
    step shorter.
    """
    with pymupdf.open(stream=content, filetype="pdf") as document:
        page = document[page_number - 1]
        left, top, right, bottom = box
        clip = pymupdf.Rect(
            left - OCR_REGION_PADDING, top - OCR_REGION_PADDING,
            right + OCR_REGION_PADDING, bottom + OCR_REGION_PADDING,
        ) * (72.0 / OCR_DPI)
        path = Path(temp_dir) / f"{name}.png"
        page.get_pixmap(dpi=OCR_DPI, clip=clip & page.rect).save(str(path))
        return path


# Character ranges per script, named the way OSD names them so the two can be compared.
TEXT_SCRIPT_RANGES = (
    ("Latin", ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F))),
    ("Cyrillic", ((0x400, 0x4FF),)),
    ("Greek", ((0x370, 0x3FF),)),
    ("Hebrew", ((0x590, 0x5FF), (0xFB1D, 0xFB4F))),
    ("Arabic", ((0x600, 0x6FF), (0xFB50, 0xFEFF))),
    ("Devanagari", ((0x900, 0x97F),)),
    ("Han", ((0x3400, 0x9FFF),)),
    ("Japanese", ((0x3040, 0x30FF),)),
)
# Japanese is written with Han characters too, so those two never contradict each other.
COMPATIBLE_SCRIPTS = {frozenset(("Han", "Japanese"))}


def dominant_text_script(text: str) -> str:
    """Which script most of a text is written in, "" when it holds no letters we know."""
    best, best_count = "", 0
    for name, spans in TEXT_SCRIPT_RANGES:
        count = sum(1 for c in text if any(low <= ord(c) <= high for low, high in spans))
        if count > best_count:
            best, best_count = name, count
    return best


def text_layer_is_trustworthy(content: bytes, page_number: int, text: str) -> bool:
    """Does the page's text layer agree with what is printed on the page?

    A PDF can carry a text layer that decodes to something else entirely - a Hebrew page whose
    font maps to Latin letters yields `hinmrg lß tilrdph`. There is no flag for it: the font has
    a ToUnicode table, it is simply wrong. But the rendered image still shows Hebrew, so asking
    OSD what script the page *looks* like and comparing that to the script the text *claims*
    catches it without any threshold or guesswork.
    """
    claimed = dominant_text_script(text)
    if not claimed or not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return True
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            printed, confidence = ocr_page_osd(render_pdf_page(content, page_number, temp_dir))
    except Exception:
        return True
    if not printed or confidence < OCR_SCRIPT_MIN_CONFIDENCE:
        # A numbers-heavy table page misread as Cyrillic at confidence 0.42-1.67 - too little
        # real-letter signal for OSD to mean anything, same as no answer at all.
        return True
    return scripts_are_compatible(printed, claimed)


def ocr_page_blocks(image_path: Path, languages: str) -> List[Dict[str, Any]]:
    """The text blocks tesseract's layout analysis found, in reading order.

    Layout analysis is driven by connected components, not by the language, so the boxes are
    usable even though `languages` is only the throwaway probe. The recognised words come along
    for free in the same call and say how much text a block holds, which decides below whether
    its own script reading can be believed.
    """
    try:
        output = subprocess.run(
            ["tesseract", str(image_path), "stdout", "-l", languages, "tsv"],
            check=True, capture_output=True, text=True,
        ).stdout
    except Exception:
        # No layout, no per-block reading: the caller falls back to reading the whole page.
        return []

    blocks: Dict[int, Dict[str, Any]] = {}
    order: List[int] = []
    for line in output.splitlines()[1:]:
        columns = line.split("\t")
        if len(columns) < 12:
            continue
        level, number = int(columns[0]), int(columns[2])
        if level == TSV_BLOCK_LEVEL:
            left, top, width, height = (int(columns[column]) for column in (6, 7, 8, 9))
            blocks[number] = {"box": (left, top, left + width, top + height), "words": []}
            order.append(number)
        elif level == TSV_WORD_LEVEL and number in blocks and columns[11].strip():
            blocks[number]["words"].append(columns[11].strip())

    found = []
    for number in order:
        block = blocks[number]
        block["text"] = " ".join(block["words"])
        if block["text"]:
            found.append(block)
    return found


def ocr_read_with_detection(image_path: Path, script: str) -> str:
    """Read an image whose language is unknown, given the script it is printed in.

    Auto-detect on a scan is a chicken-and-egg: detection needs text, text needs OCR, OCR needs
    the language. So read once with a few languages of that script. That result is thrown away
    and only has to be good enough for langdetect; the image is then read again with the single
    language langdetect named.
    """
    probe = ocr_probe_languages(script)
    text = run_tesseract(image_path, probe)
    if not text:
        return text
    read_with = probe
    # Two reads at most on top of the probe. The probe mangles diacritics, which is enough to
    # make langdetect pick a close relative - a Czech page came back as Slovak - so the guess
    # is checked once more against the clean text the chosen language produced.
    for _ in range(2):
        code = ocr_language_code(detect_source_language(text))
        if code == read_with:
            break
        better = run_tesseract(image_path, code)
        if not better:
            break
        text, read_with = better, code
    return text


def ocr_block_scripts(content: bytes, page_number: int, blocks: List[Dict[str, Any]],
                      page_script: str, temp_dir: str) -> List[Tuple[str, Optional[Path]]]:
    """The script each block is printed in, one entry per block, alongside the region image
    already rendered to determine it (None for blocks too short to be asked at all) - so a mixed
    page's later read of that same block doesn't render it from the PDF a second time.

    OSD needs a fair amount of text before its answer means anything, and it does not say so - it
    answers anyway. Measured on the scanned fixtures: blocks of 200+ characters were right every
    time, while a 17-character Japanese heading came back "Arabic" and a 9-character one "Han".
    Reading those in the script OSD named would have replaced the heading with invented Arabic,
    so short blocks are not asked at all and take the page's script instead.
    """
    results = []
    for index, block in enumerate(blocks):
        script, region = "", None
        if len(block["text"]) >= OCR_BLOCK_MIN_CHARS:
            region = render_pdf_region(content, page_number, block["box"], temp_dir, f"block{index}")
            script = ocr_page_script(region)
        if not ocr_script_is_usable(script) or scripts_are_compatible(script, page_script):
            # Either OSD named something no language can be picked for - a Chinese page had one
            # block come back "Korean", which ocr_probe_languages can only answer with English,
            # and reading Han as English deletes it - or it named a script that is written with
            # the page's anyway (kanji in a Japanese page). Neither is a reason to split.
            script = page_script
        results.append((script, region))
    return results


def ocr_pdf_page(content: bytes, page_number: int, source: str = AUTO_SOURCE) -> str:
    ensure_ocr_tools()
    with tempfile.TemporaryDirectory() as temp_dir:
        image_path = render_pdf_page(content, page_number, temp_dir)
        if source != AUTO_SOURCE:
            return run_tesseract(image_path, ocr_language_code(source))

        page_script = ocr_page_script(image_path)
        blocks = ocr_page_blocks(image_path, ocr_probe_languages(page_script))
        block_scripts = ocr_block_scripts(content, page_number, blocks, page_script, temp_dir)
        if len({script for script, _region in block_scripts}) < 2:
            # One script on the page, which is the normal case: read it in one go. Splitting a
            # page into blocks only to read each in the same language would cost several OCR
            # runs and lose the layout analysis' view of the whole page for nothing.
            return ocr_read_with_detection(image_path, page_script)

        # A mixed page. OSD picks one script for the whole image, so reading it in one go always
        # loses the other script's text: the majority wins and the rest is either dropped or
        # reinvented in the wrong alphabet. Each block is read in its own script instead and the
        # results are put back together in tesseract's reading order.
        pieces = []
        for index, (block, (script, region)) in enumerate(zip(blocks, block_scripts)):
            if region is None:
                region = render_pdf_region(content, page_number, block["box"], temp_dir, f"read{index}")
            text = ocr_read_with_detection(region, script)
            if text.strip():
                pieces.append(text.strip())
        return "\n\n".join(pieces)


def extract_pdf_markdown_from_bytes(
    content: bytes,
    content_type: str = "application/pdf",
    page_range: str = "",
    source: str = AUTO_SOURCE,
) -> str:
    if content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")

    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"PDF exceeds {MAX_FILE_MB} MB")

    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}") from exc

    try:
        if not document.page_count:
            raise HTTPException(status_code=422, detail="PDF has no pages")

        selected_pages = parse_page_range(page_range, document.page_count)
        pages = []
        pages_with_text = 0
        # Checked once on the first page that has one, not per page: a text layer is written by one
        # producer for the whole file, and the check costs a render plus an OSD call.
        trust_text_layer = None
        for index in selected_pages:
            text = document[index - 1].get_text() or ""
            text = re.sub(r"[ \t]+\n", "\n", text).strip()
            needs_ocr = not text or len(text) < PDF_LOW_TEXT_CHARS
            if not needs_ocr:
                if trust_text_layer is None:
                    trust_text_layer = text_layer_is_trustworthy(content, index, text)
                needs_ocr = not trust_text_layer
            if needs_ocr:
                ocr_text = ocr_pdf_page(content, index, source)
                if ocr_text:
                    text = ocr_text
                    if source == AUTO_SOURCE:
                        # Keep what the first scanned page turned out to be: the pages after it then
                        # read in one pass instead of probing the same document over and over.
                        source = detect_source_language(ocr_text)

            if text:
                pages_with_text += 1
                pages.append(f"# Page {index}\n\n{text}".strip())
            else:
                pages.append(f"# Page {index}\n\n> No extractable text found on this page. It may need OCR.")
    finally:
        document.close()

    markdown = "\n\n".join(pages).strip()
    if not pages_with_text:
        raise HTTPException(
            status_code=422,
            detail=f"No extractable text found. This PDF may be scanned, image-only, or protected. "
                   f"OCR ran with {ocr_language_code(source)}, but no readable text was produced.",
        )
    return markdown


def extract_pdf_page_images(content: bytes) -> Dict[str, Dict[str, Any]]:
    """Every page's own images and page size, keyed by page number as a string - matching the
    "# Page N" markers pdf_sections splits translated markdown on, so create_text_pdf can place a
    PDF's original images on its re-exported translation (pdf_page_images) without threading
    anything through the job pipeline: this just re-opens the same source bytes history already
    keeps around and reads positions fresh, the way export_pdf_layout_with_translated_text does
    for the layout pipeline's own history re-export.

    Never raises - a page whose bytes can no longer be parsed keeps the plain, image-less export
    rather than failing a translation that already exists.
    """
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception:
        return {}
    try:
        info = {}
        for index in range(document.page_count):
            page = document[index]
            images = pdf_page_images(page)
            if images:
                info[str(index + 1)] = {
                    "images": images,
                    "width": float(page.rect.width),
                    "height": float(page.rect.height),
                }
        return info
    finally:
        document.close()


MUPDF_BOLD_FLAG = 1 << 4  # span flag bit 4, per PyMuPDF's text-extraction flag table
MUPDF_ITALIC_FLAG = 1 << 1  # bit 1 of the same table

# Hebrew, Arabic, Syriac, Thaana, NKo and the Arabic presentation forms. Devanagari (0900-097F)
# is deliberately outside: it runs left to right.
RTL_RANGES = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))


# How much of the smaller of two text boxes has to be inside the larger before they count as the
# same ink. Well below full containment, since a duplicate layer is usually drawn with a slightly
# different font and lands a point or two off.
PDF_RUN_OVERLAP = 0.5


def overlaps_mostly(box, other) -> bool:
    smaller = min(box.get_area(), other.get_area())
    return smaller > 0 and (box & other).get_area() > PDF_RUN_OVERLAP * smaller


def is_rtl_char(char: str) -> bool:
    return any(low <= ord(char) <= high for low, high in RTL_RANGES)


def is_rtl_text(text: str) -> bool:
    """Whether a string is mostly written right to left."""
    rtl = sum(1 for char in text if is_rtl_char(char))
    return rtl > 0 and rtl * 2 > sum(1 for char in text if char.isalpha())


def direction_segments(text: str) -> List[Tuple[str, bool]]:
    """The line cut into stretches of one writing direction, each with the direction it runs in.

    A Hebrew line holding a year or a product name is two directions at once, and both the
    reader and the writer have to treat those stretches separately: the line as a whole turns
    around, the Latin word inside it does not.
    """
    segments: List[List[str]] = []
    directions: List[bool] = []
    for char in text:
        rtl = is_rtl_char(char)
        # Only a letter or digit switches direction. A space between two Hebrew words is not a
        # segment of its own, otherwise the two words swap places.
        if segments and (not char.isalnum() or rtl == directions[-1]):
            segments[-1].append(char)
        else:
            segments.append([char])
            directions.append(rtl)
    # Punctuation at the very start has nothing before it to belong to and would stand as a
    # stretch of its own, running the wrong way: the full stop and closing quote at the left end
    # of a Hebrew line came back as `היער.'` instead of `היער'.`. It belongs to the line it ends.
    if len(segments) > 1 and not any(char.isalnum() for char in segments[0]):
        segments[1] = segments[0] + segments[1]
        del segments[0], directions[0]
    return [("".join(chars), rtl) for chars, rtl in zip(segments, directions)]


def reverse_rtl(text: str) -> str:
    """Reverse a stretch of right-to-left text without tearing its marks off.

    A vowel point or an Arabic harakat follows the letter it belongs to and carries no width of
    its own, so reversing character by character drops it behind the letter before: measured on
    a real Arabic page, `معروفًا` came back as `معروًفا`.
    """
    clusters: List[str] = []
    for char in text:
        if clusters and unicodedata.category(char) == "Mn":
            clusters[-1] += char
        else:
            clusters.append(char)
    return "".join(reversed(clusters))


def visual_to_logical(text: str) -> str:
    """Turn a right-to-left line from the order it stands on the page into reading order.

    The layout pipeline assembles a line from left to right, which for Hebrew or Arabic is the
    order a reader ends on. Reversing it restores the reading order the model needs; digits and
    Latin words inside the line already run left to right and are reversed back into place, and
    spaces or punctuation stay with the segment they were found in.
    """
    parts = [(reverse_rtl(part) if rtl else part).strip()
             for part, rtl in direction_segments(text)]
    logical = " ".join(part for part in reversed(parts) if part)
    # A converter that lays an RTL paragraph out without running the bidi algorithm leaves the
    # sentence's full stop at the right edge of the line, where it stands in the file, instead
    # of the left edge where it is read. It then arrives here as the first character of the
    # line. No line of prose begins with a full stop, so it goes back to the end where it
    # belongs - measured on a Word document converted by Stirling-PDF, this was 40 of its 45
    # differences against the original text.
    lead = 0
    while lead < len(logical) - 1 and logical[lead] in ".!?'\"":
        lead += 1
    # A closing quote goes back with the full stop it stands next to. Only a stretch holding an
    # end-of-sentence mark is moved, so a line that genuinely opens with a quotation stays put.
    if any(char in ".!?" for char in logical[:lead]):
        logical = logical[lead:].lstrip() + logical[lead - 1::-1]
    return logical


def pdf_page_flip_matrix(page):
    """The Y-flip between MuPDF's rendered coordinates (down from the top) and the reading-space
    coordinates the rest of the layout pipeline works in.

    get_text, get_drawings and get_image_rects report positions in the page's own raw, un-rotated
    content-stream space - matching `page.mediabox`, not `page.rect` - /Rotate is a display
    instruction, not a transform on the coordinates those calls hand back (re-measured
    15.08.2026: on a /Rotate 270 page, a footer's own raw y-origin landed near the *top* of
    `page.mediabox.height`, not `page.rect.height`, and every extracted paragraph came out
    shifted, several lines running to negative y). Equal to `page.transformation_matrix` always -
    both are a plain flip by the raw mediabox height, rotation left for the pages/viewer to apply
    on top of content that is built and merged in this same raw frame, see extract_pdf_layout and
    render_pdf_layout_overlay.
    """
    return pymupdf.Matrix(1, 0, 0, -1, 0, page.mediabox.height)


def pdf_page_runs(page, rules: Optional[List[Dict[str, float]]] = None,
                  widget_rects: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    """Every text run on a PyMuPDF page, positioned in PDF user space.

    MuPDF hands over text already split into lines and spans, each with its baseline origin,
    measured bounding box, rendered size and font flags. Its own coordinates count downwards
    from the top-left of the page, so every point is mapped back through the inverse page
    transformation into the user space the overlay is later drawn in.

    Read as `rawdict` rather than `dict` for the position of every single character: a span's
    ready-made text follows the order the page paints in, which for right-to-left text is the
    producer's business and not something to rely on. Sorting the characters by their own x
    gives the order they stand in on the page, whoever wrote the file.

    `widget_rects` excludes an AcroForm field's own value, drawn by the widget's appearance
    stream rather than the page's content stream: extracting it here would translate it as an
    ordinary paragraph and draw the result where redaction cannot reach it, invisibly behind the
    widget's original value, see pdf_page_widget_values.
    """
    runs: List[Dict[str, Any]] = []
    # ponytail: O(runs^2) per page, a few hundred runs at most; index by row if a page ever
    # shows up where it isn't.
    boxes: List[Any] = []
    inverse = ~pdf_page_flip_matrix(page)
    # ponytail: rawdict carries a dict per character, heavier than dict on long documents;
    # narrow it to pages that hold right-to-left text if extraction ever shows up in a profile.
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            # The overlay is drawn horizontally; rotated or vertical text keeps its original.
            if abs(line["dir"][1]) > 0.01 or line["dir"][0] <= 0:
                continue
            for span in line["spans"]:
                characters = span["chars"]
                right_to_left = any(is_rtl_char(item["c"]) for item in characters)
                if right_to_left:
                    characters = sorted(characters, key=lambda item: item["origin"][0])
                text = "".join(item["c"] for item in characters)
                if not text.strip():
                    continue
                x, y = pymupdf.Point(span["origin"]) * inverse
                if right_to_left:
                    # A right-to-left span starts where it is read from, its right edge, and
                    # that is the origin MuPDF reports. Everything downstream measures a line
                    # from its left edge: a sentence's full stop, drawn as its own span, sorted
                    # to the far right of the text it ends and came out in front of the next
                    # line instead.
                    x, _ = pymupdf.Point(span["bbox"][0], span["origin"][1]) * inverse
                if abs(x) < 0.01 and abs(y) < 0.01:
                    # Some generators dump a hidden duplicate of the page's text anchored at the
                    # origin (accessibility/search layer). It is never real, visible content.
                    continue
                # Text drawn on top of text that is already there. Two sources of it: synthetic
                # bold, where the same glyphs are painted twice at the same spot to fake a weight
                # the font does not have (one copy is enough, or lines double up into
                # "PPoowweerr"), and generators that leave a second full copy of the page's text
                # behind, offset and often with a broken encoding. Only one of them can be the
                # text the reader sees, so the first one drawn is kept and later ones covering
                # the same ink are dropped: extracting both would translate the page twice and
                # stamp the second translation across the first.
                box = pymupdf.Rect(span["bbox"])
                if widget_rects and any(overlaps_mostly(box, rect) for rect in widget_rects):
                    continue
                if any(overlaps_mostly(box, taken) for taken in boxes):
                    continue
                # Recorded before the direction check, not after: an RTL run is ink on the page
                # like any other, and a duplicate layer drawn over it has to be recognised as the
                # duplicate it is. Skipping it earlier left the second copy without a partner to
                # match against, and it came through as text in its own right.
                boxes.append(box)
                # A producer may paint two cells of a table row in a single run, with nothing
                # but a few spaces between them ("Objekt   Mensch  -  " on MatterhornProtokoll,
                # 0.59em apart, which is ordinary word spacing in justified text and cannot be
                # told apart by width). The rule drawn between them can, so the run is cut where
                # the page's own grid says the cell ends.
                for part in split_run_at_rules(characters, y, inverse, rules):
                    runs.append({
                        # A span nobody cut keeps the origin and width MuPDF reported for it, so
                        # this changes nothing for a document without a table grid.
                        "text": part["text"],
                        "x": part["x"] if part["split"] else x,
                        "y": y,
                        "size": span["size"],
                        "width": (part["width"] if part["split"]
                                  else span["bbox"][2] - span["bbox"][0]),
                        "bold": bool(span["flags"] & MUPDF_BOLD_FLAG),
                        "italic": bool(span["flags"] & MUPDF_ITALIC_FLAG),
                        "serif": pdf_font_is_serif(span["font"]),
                        # sRGB packed into an int by MuPDF, 0 being black. Without it a title set
                        # in white on a dark cover image comes back drawn in the default black.
                        "color": span.get("color", 0),
                    })
    return runs


def split_run_at_rules(characters: List[Dict[str, Any]], y: float, inverse,
                       rules: Optional[List[Dict[str, float]]]) -> List[Dict[str, Any]]:
    """Cut a span's characters into cells wherever a drawn column rule stands between them.

    Returns one part for a span no rule crosses, which is every span of a document without a
    table grid. `split` marks the parts of a span that really was cut, so the caller knows the
    part's own left edge has to be used rather than the span's reported origin.
    """
    boxes = [pymupdf.Rect(item["bbox"]) * inverse for item in characters]
    left, right = min(box.x0 for box in boxes), max(box.x1 for box in boxes)
    crossing = sorted(rule["x"] for rule in rules or []
                      if left + 1 < rule["x"] < right - 1 and rule["bottom"] <= y <= rule["top"])

    parts, current = [], []
    for item, box in zip(characters, boxes):
        while crossing and box.x0 >= crossing[0]:
            crossing.pop(0)
            # Only where a space sits on the boundary. A cell whose text overhangs its own rule
            # by a hair would otherwise be cut inside a word: the "Siehe" entries of
            # MatterhornProtokoll start 5pt left of their column's line and came apart into "0"
            # and "1-001".
            if current and current[-1][0].isspace():
                parts.append(current)
                current = []
        current.append((item["c"], box))
    if current:
        parts.append(current)

    out = []
    for part in parts:
        text = "".join(character for character, _ in part)
        if not text.strip():
            continue
        edges = [box for _, box in part]
        out.append({"text": text, "x": min(box.x0 for box in edges),
                    "width": max(box.x1 for box in edges) - min(box.x0 for box in edges),
                    "split": len(parts) > 1})
    return out or [{"text": "".join(item["c"] for item in characters), "x": left,
                    "width": right - left, "split": False}]


# Runs on one baseline further apart than this (in multiples of the run's own font size) belong
# to separate cells of a table row rather than to one sentence. Wide enough to leave tab stops
# and justified word spacing inside a line alone.
PDF_CELL_GAP = 2.0
# How many runs (or, in page_column_walls, lines) of a page have to begin at the same x before
# that x counts as a column a table is set in. A run starting on one opens a cell of its own
# however close it sits to the run before it, and no line is reflowed past one.
# The gap alone is not enough: the two columns of Powerupall's page 72 stand 0.68em apart, well
# inside PDF_CELL_GAP, so every row of it was merged into one line and reflowed across the table.
# Measured (Aug 2026) over the nine test documents: this catches all 132 cell boundaries of
# MatterhornProtokoll, the five broken rows of page 72 and eight more real cells, and nothing else.
PDF_COLUMN_MIN_RUNS = 3
# How tall a vertical hairline has to be before pdf_page_rules reads it as a table's column rule.
# Half a line: below that it is a tick, a bullet or a piece of an icon, and MatterhornProtokoll's
# own row rules are 18.6pt tall.
PDF_RULE_MIN_HEIGHT = 6.0
# How close a column's own text has to sit to a drawn vertical rule before group_pdf_paragraphs
# trusts it as that rule's own cell (ordinary left-padding inside a table cell) rather than an
# unrelated column that happens to sit somewhere past it. Landscape_Mixed_Pages' own header cells
# measured at 6.4pt.
PDF_TABLE_BORDER_PADDING = 10.0
# How close two of page_column_walls' own columns have to sit before group_pdf_paragraphs treats
# them as one - a page number column measured a few points off its title column's x, or a header
# cell's padding differing a hair from the rule beside it, would otherwise count as two columns
# and throw off the row-completeness check below.
PDF_TABLE_ROW_COLUMN_CLUSTER = 12.0
# The largest share of a row's active columns a genuine wrapped continuation may still have
# content in, before group_pdf_paragraphs reads it as a full new row instead. A table header cell
# wrapped to a second line only ever fills the few columns whose own header happened to wrap
# (Landscape_Mixed_Pages: "velocity (m/s)" / "(MW)", 2 of 10 columns, 0.2) - a real new row of the
# table fills all or nearly all of them (Table_Across_Pages: 6 of 6, 1.0).
PDF_TABLE_ROW_MAX_FILL = 0.5


def pdf_page_obstacles(page) -> List[Dict[str, float]]:
    """Everything on the page that is not text but still occupies room, in PDF user space.

    The reflow only ever measured itself against other *text*, so a photograph and a table cell
    did not exist as far as it was concerned: translations ran straight across the images of
    Stall-Kamera-System and out of the boxes of Systemrequirements. Both are the same gap, and
    both are closed by handing paragraph_floor and paragraph_width_limit the page's images and
    filled shapes alongside its paragraphs.

    Hairlines are left out. A table rule reserves no room worth having, and an underline sits a
    fraction of an em below its own baseline - taken as a floor it would stop the paragraph it
    belongs to from growing at all.
    """
    inverse = ~pdf_page_flip_matrix(page)
    rects = [rect for image in page.get_images(full=True)
             for rect in page.get_image_rects(image[0])]
    rects += [drawing["rect"] for drawing in page.get_drawings()]
    obstacles = []
    for rect in rects:
        mapped = pymupdf.Rect(rect) * inverse
        if mapped.width < 2 or mapped.height < 2:
            continue
        obstacles.append({"x": mapped.x0, "right": mapped.x1,
                          "top": mapped.y1, "bottom": mapped.y0})
    return obstacles


def pdf_page_images(page) -> List[Dict[str, Any]]:
    """Every image on the page with its own raw bytes, positioned in PDF user space like
    pdf_page_obstacles - except this is for drawing the image back, not just avoiding it, see
    extract_pdf_page_images. PDF_IMAGE_MIN_SIZE drops icons and decorative dividers the same way
    pdf_page_obstacles drops hairlines.
    """
    inverse = ~pdf_page_flip_matrix(page)
    images = []
    for xref, *_ in page.get_images(full=True):
        try:
            data = page.parent.extract_image(xref)["image"]
        except Exception:
            continue
        for rect in page.get_image_rects(xref):
            mapped = pymupdf.Rect(rect) * inverse
            if mapped.width < PDF_IMAGE_MIN_SIZE or mapped.height < PDF_IMAGE_MIN_SIZE:
                continue
            images.append({"bytes": data, "x": mapped.x0, "right": mapped.x1,
                           "top": mapped.y1, "bottom": mapped.y0})
    return images


def pdf_page_rules(page) -> List[Dict[str, float]]:
    """The vertical hairlines of the page: the drawn grid of a table, in PDF user space.

    pdf_page_obstacles drops these on purpose - a rule reserves no room a paragraph could use.
    For telling cells apart they are the best signal there is, though, better than any gap: the
    Index and Fehlerbedingung columns of MatterhornProtokoll stand 2pt apart and a producer even
    paints both cells of a row in one text run ("Objekt   Mensch  -  "), while the line between
    them is drawn, unambiguous and exactly where the boundary is.
    """
    inverse = ~pdf_page_flip_matrix(page)
    rules = []
    for drawing in page.get_drawings():
        mapped = pymupdf.Rect(drawing["rect"]) * inverse
        # Upright and thin: a horizontal hairline is an underline or a table's own row rule, and
        # says nothing about where one cell ends and the next begins.
        if mapped.width < 2 and mapped.height >= PDF_RULE_MIN_HEIGHT:
            rules.append({"x": (mapped.x0 + mapped.x1) / 2,
                          "bottom": mapped.y0, "top": mapped.y1})
    return rules


def pdf_page_row_rules(page) -> List[Dict[str, float]]:
    """The horizontal hairlines of the page: a table's own row rules, in PDF user space.

    The mirror of pdf_page_rules, for the other axis. A table whose grid is drawn entirely in
    hairlines rather than filled cells - MatterhornProtokoll's, where even the row dividers are
    0.48pt-thick fills - leaves nothing in pdf_page_obstacles at all, that function drops
    hairlines on purpose. Column width still had page_column_walls to fall back on for an empty
    cell, but nothing stood in for a row's own lower edge: a cell whose translation grew past its
    single original line, with no neighbouring paragraph directly below it in the same column, ran
    straight through the drawn row rule into the row beneath - see enclosing_box_bottom, which is
    where this is used.
    """
    inverse = ~pdf_page_flip_matrix(page)
    rules = []
    for drawing in page.get_drawings():
        mapped = pymupdf.Rect(drawing["rect"]) * inverse
        # Level and wide: a vertical hairline is a column rule (pdf_page_rules) or a letter's own
        # stroke, and says nothing about where one row ends and the next begins.
        if mapped.height < 2 and mapped.width >= PDF_RULE_MIN_HEIGHT:
            rules.append({"y": (mapped.y0 + mapped.y1) / 2, "left": mapped.x0, "right": mapped.x1})
    return rules


def rule_between(left_start: float, right_start: float, y: float,
                 rules: Optional[List[Dict[str, float]]]) -> bool:
    """Whether a drawn vertical rule stands between where two runs on one baseline begin.

    Measured from where each run starts, not from the end of the first: a producer pads a cell
    with trailing spaces, and MatterhornProtokoll's index cells run a point past their own rule
    that way. A rule inside a cell does not exist - that is what makes it a cell boundary - so
    taking the whole span from one run's start to the next costs nothing.
    """
    return any(left_start < rule["x"] < right_start and rule["bottom"] <= y <= rule["top"]
               for rule in rules or [])


def obstacle_between(gap_left: float, gap_right: float, y: float, size: float,
                     obstacles: Optional[List[Dict[str, float]]]) -> bool:
    """Whether a filled shape or image has an edge inside the gap between two runs.

    A panel edge separates what stands on either side of it as surely as a wide gap does, and
    more reliably: on page 86 of Powerupall a caption sitting on its own tinted panel came within
    21.4pt of the body line beside it, just inside the 22pt PDF_CELL_GAP, so the two merged into
    one line. That line then reached across the panel, and the translation was drawn over it.
    """
    for obstacle in obstacles or []:
        if obstacle["bottom"] >= y + 0.8 * size or obstacle["top"] <= y:
            continue
        if gap_left < obstacle["x"] < gap_right or gap_left < obstacle["right"] < gap_right:
            return True
    return False


def group_pdf_lines(runs: List[Dict[str, Any]],
                    obstacles: Optional[List[Dict[str, float]]] = None,
                    rules: Optional[List[Dict[str, float]]] = None) -> List[Dict[str, Any]]:
    """Merge runs that share a baseline into lines, keeping table cells apart.

    Grouping by baseline is the one grouping PDFs make reliable, which is why the layout
    pipeline builds on it instead of trying to detect blocks or columns geometrically. But a
    baseline also holds every cell of a table row, and bridging those with a space merges the
    row into one line whose translation is then reflowed across the whole table width, printed
    over the neighbouring columns.
    """
    # The x positions the page sets column after column at, see PDF_COLUMN_MIN_RUNS. Rounded to
    # half a point, the same way baselines are: a column is drawn to the same coordinate every
    # time, but it still arrives with the odd hundredth of a point of drift.
    column_starts = Counter(round(run["x"] * 2) / 2 for run in runs)
    columns = {x for x, count in column_starts.items() if count >= PDF_COLUMN_MIN_RUNS}

    baselines: List[List[Dict[str, Any]]] = []
    for run in sorted(runs, key=lambda item: -item["y"]):
        if baselines and abs(baselines[-1][0]["y"] - run["y"]) <= max(1.0, 0.3 * run["size"]):
            baselines[-1].append(run)
        else:
            baselines.append([run])

    lines: List[Dict[str, Any]] = []
    for baseline in baselines:
        cells: List[Dict[str, Any]] = []
        for run in sorted(baseline, key=lambda item: item["x"]):
            width = run.get("width") or pdf_measure_text(run["text"], run["size"])
            current = cells[-1] if cells else None
            gap = run["x"] - current["right"] if current else 0.0
            # A rule to write an answer on is a field of the form, not the end of the sentence
            # beside it. It is kept apart however close it sits, because the gap that normally
            # tells cells apart is whatever the entry before it happened to leave: on the
            # Powerupall answer sheet 27 of the 28 rules stood 44pt or more from their item and
            # were kept, while item 13 has the longest wording on the page and left 9pt, so its
            # rule was merged into the item, reflowed with the translation and moved.
            # A rule drawn between the two is the page's own grid saying where the cell ends, and
            # beats every measurement of the gap: the Index and Fehlerbedingung columns of
            # MatterhornProtokoll stand 2pt apart, far inside anything a gap rule would catch.
            #
            # A run opening one of the page's columns starts a cell of its own, but only where it
            # is really set apart from what precedes it - half an em, more than a word space and
            # less than the narrowest cell gap measured - and only after a cell holding more than
            # a list marker or a number, which hangs to the left of its own text and belongs with
            # it. Without both, a comma mid-sentence that happened to fall on a column split a
            # Powerupall paragraph in two.
            column = (gap > 0.5 * run["size"]
                      and round(run["x"] * 2) / 2 in columns
                      and len(current["text"].strip()) > 3)
            if current and not column \
                    and not FORM_RULE.fullmatch(run["text"].strip()) \
                    and not FORM_RULE.fullmatch(current["text"].strip()) \
                    and not obstacle_between(current["right"], run["x"], run["y"], run["size"],
                                             obstacles) \
                    and not rule_between(current["x"], run["x"], run["y"], rules) \
                    and gap <= PDF_CELL_GAP * run["size"]:
                separator = " " if gap > 0.2 * run["size"] and not current["text"].endswith(" ") else ""
                current["text"] += separator + run["text"]
                current["right"] = max(current["right"], run["x"] + width)
                current["size"] = max(current["size"], run["size"])
            else:
                current = {
                    "text": run["text"],
                    "x": run["x"],
                    "y": run["y"],
                    "right": run["x"] + width,
                    "size": run["size"],
                    "bold_chars": 0,
                    "serif_chars": 0,
                    "italic_chars": 0,
                    "color_chars": Counter(),
                    "total_chars": 0,
                }
                cells.append(current)
            # A line can mix faces (a bold lead-in followed by regular text). Weighted by
            # characters so the face most of the line is set in decides how it is drawn.
            length = len(run["text"].strip())
            current["total_chars"] += length
            if run.get("bold"):
                current["bold_chars"] += length
            if run.get("serif"):
                current["serif_chars"] += length
            if run.get("italic"):
                current["italic_chars"] += length
            current["color_chars"][run.get("color", 0)] += length
        for cell in cells:
            # Expanded here rather than per run: a producer often draws the ligature from its own
            # font, so it arrives as a span of its own and the letter before it - which is what
            # tells a mis-mapped ligature from a real capital - sits in the previous one.
            cell["text"] = expand_pdf_ligatures(re.sub(r"\s+", " ", cell["text"]).strip())
            # The single point where reading order is restored: the cell has just been put
            # together from left to right, out of runs that stand in page order, so this is the
            # only place the whole visual line exists. Doing it per run instead would reverse
            # each run on its own and leave them in the wrong order relative to each other.
            if is_rtl_text(cell["text"]):
                cell["text"] = visual_to_logical(cell["text"])
            total = cell.pop("total_chars")
            cell["bold"] = cell.pop("bold_chars") * 2 > total
            cell["serif"] = cell.pop("serif_chars") * 2 > total
            # A word or two of italic inside a sentence cannot survive translation - nobody knows
            # which words of the answer correspond to it - so the line is italic only when most of
            # it is, the same majority the weight is decided by.
            cell["italic"] = cell.pop("italic_chars") * 2 > total
            colors = cell.pop("color_chars")
            cell["color"] = colors.most_common(1)[0][0] if colors else 0
        # A justified Hebrew line whose word spacing grows past PDF_CELL_GAP is cut into cells
        # like a table row, and those are read from the right: taken left to right, the end of
        # the sentence came before its beginning and the full stop landed in front of the next
        # line's first word.
        if is_rtl_text(" ".join(cell["text"] for cell in cells)):
            cells.reverse()
        lines.extend(cell for cell in cells if cell["text"])
    return lines


def typical_line_spacing(lines: List[Dict[str, Any]]) -> float:
    """The page's own leading: the smallest gap between lines that it uses more than once.

    A page set at one and a half or double spacing leaves gaps no multiple of the font size
    recognises as "the next line of this paragraph" - the dedication page of the test document
    runs at 2.35 times its 11pt, and every line of it came out as a paragraph of its own.

    Smallest rather than most common, because the gap between two paragraphs is a repeated one
    too and often the more frequent of the two: a page of short paragraphs has more gaps between
    them than inside them, and taking the average or the most common gap would then swallow every
    paragraph break on the page. Gaps are rounded to half a point, PDF baselines wobble.
    """
    gaps = Counter(round((above["y"] - below["y"]) * 2) / 2
                   for above, below in zip(lines, lines[1:]) if above["y"] > below["y"])
    repeated = [gap for gap, count in gaps.items() if count > 1]
    return min(repeated) if repeated else 0.0


# Ceiling on the leading/size ratio a line may sit at and still extend a paragraph that is not
# the immediately preceding one (see group_pdf_paragraphs). Below the ordinary 1.8x tolerance
# because that gap is the only thing standing between a genuine reconnection and a table's own
# row-to-row gap once the immediate-only check is relaxed. Measured (Aug 2026): every real
# continuation across Two_Column_Paper's two columns and Powerupall's page 72 list sat at
# 1.15-1.33x; every false candidate - MatterhornProtokoll's Index, Version and Abschnitt columns,
# row to row - sat at 1.74-1.77x. 1.5 splits the two with room on both sides.
PDF_LAYOUT_CONTINUATION_MAX_RATIO = 1.5
# Share of a column's own lines that has to end at that column's single most common right edge,
# and how wide that edge has to sit past the column's own left edge, before group_pdf_paragraphs
# trusts a gap enough to look past the immediately preceding paragraph for it. Ratio alone still
# lets two false positives through: a table column of repeated short words ("Objekt"/"Objekt")
# hits a high share purely by having little to vary, and a page-number column hits 100% while
# being a few points wide. Width alone still passes MatterhornProtokoll's Fehlerbedingung column
# (191-349pt even on single-line rows). Measured (Aug 2026): Two_Column_Paper's two columns and
# Powerupall page 72's indented paragraph opener sat at 86-93% repeat share and 209-486pt width;
# every column of MatterhornProtokoll's tables and TOC, and Powerupall's own two list columns
# (33-42%, ragged - each entry a different length), missed one or the other by a wide margin.
PDF_LAYOUT_CONTINUATION_MIN_JUSTIFIED_SHARE = 0.5
PDF_LAYOUT_CONTINUATION_MIN_WIDTH = 100.0


def pdf_layout_justified_columns(lines: List[Dict[str, Any]]) -> Set[int]:
    """The x's (rounded to the point) whose lines mostly end at the same right edge.

    Evidence that a column is body text set flush, not a table cell or list entry - those wrap
    ragged, a different length practically every time. See group_pdf_paragraphs.
    """
    by_x: Dict[int, List[int]] = {}
    for line in lines:
        by_x.setdefault(round(line["x"]), []).append(round(line["right"]))
    columns = set()
    for x, rights in by_x.items():
        if len(rights) < 3:
            continue
        edge, count = Counter(rights).most_common(1)[0]
        if (count / len(rights) >= PDF_LAYOUT_CONTINUATION_MIN_JUSTIFIED_SHARE
                and edge - x >= PDF_LAYOUT_CONTINUATION_MIN_WIDTH):
            columns.add(x)
    return columns


def group_pdf_paragraphs(lines: List[Dict[str, Any]],
                         rules: Optional[List[Dict[str, float]]] = None) -> List[Dict[str, Any]]:
    """Bundle lines into paragraphs, purely so the model gets whole sentences.

    A wrong split only costs translation quality here, never placement: every line keeps its
    own coordinates and the translation is reflowed into exactly those.
    """
    # Allowed with a bit of room over the page's own leading, so an ordinary line of the same
    # paragraph still fits while the wider gap before the next one does not.
    spacing_limit = 1.25 * typical_line_spacing(lines)

    def paragraph_fits_line(current: Dict[str, Any], line: Dict[str, Any],
                            size_ratio: float = 1.8) -> bool:
        previous = current["lines"][-1]
        spacing = previous["y"] - line["y"]
        # A paragraph's first line rarely sits where the rest of it does, and it is the second
        # line that sets the real left edge: measured against that once it exists. Until then,
        # a line further left continues an indented opening line, and a line further right
        # continues a list item, whose marker hangs out to the left of its own text.
        first = len(current["lines"]) == 1
        left = previous["x"] if first else current["lines"][1]["x"]
        # A centred block shares no left edge at all - each line is only as wide as its own
        # text - but its centre stays put line to line, the same quantity paragraph_center
        # measures later to reflow it. Without this, a multi-line centred heading split into
        # one single-line "paragraph" per line, each translated with no context from the rest.
        reference = previous if first else current["lines"][1]
        reference_centre = (reference["x"] + reference["right"]) / 2
        line_centre = (line["x"] + line["right"]) / 2
        aligned = (
            abs(left - line["x"]) <= 3
            or abs(reference_centre - line_centre) <= 3
            or (first and 0 < previous["x"] - line["x"] <= PDF_LAYOUT_MAX_INDENT)
            or (first and PDF_LIST_MARKER.match(previous["text"])
                and 0 < line["x"] - previous["x"] <= PDF_LAYOUT_MAX_INDENT)
        )
        return (
            0 < spacing <= max(size_ratio * max(previous["size"], line["size"]), spacing_limit)
            and aligned
            # A marker opens an item, so it can only ever open a paragraph too. Consecutive
            # one-line items are indistinguishable from a wrapped paragraph by geometry
            # alone: same left edge, same leading, and a whole list came out as prose.
            and not PDF_LIST_MARKER.match(line["text"])
            and abs(previous["size"] - line["size"]) <= 0.2 * previous["size"]
            # A change of weight ends a paragraph just as a change of size does. Not every
            # document sets its headings larger: Get_Started_With_Smallpdf sets them bold at
            # the body size, so they were swallowed by the paragraph below and lost both their
            # own weight (paragraph_base_size and the bold vote go by majority) and their own
            # alignment. Safe against a bold lead-in inside a sentence, because a line's own
            # weight is already decided by which face most of its characters use.
            and bool(previous.get("bold")) == bool(line.get("bold"))
        )

    justified_columns = pdf_layout_justified_columns(lines)

    # A column with a drawn rule close to its own left edge sits inside an actual bordered table
    # cell. On its own this is not enough evidence a wrapped line belongs with the one above it -
    # a real table's own row-to-row gap can be every bit as tight as a wrapped cell's own two
    # lines (Table_Across_Pages) - so it is only ever used together with row_is_partial below.
    bordered_columns = [rule["x"] for rule in rules or []]

    def has_cell_border(x: float) -> bool:
        return any(abs(rule_x - x) <= PDF_TABLE_BORDER_PADDING for rule_x in bordered_columns)

    # The page's real columns (page_column_walls), clustered so a page number a few points off
    # its title column, or a header cell's padding differing a hair from the rule beside it,
    # still reads as the one column it is. Each cluster keeps every wall's own (bottom, top) span
    # rather than collapsing to just an x: a busy page can hold more than one table, and a column
    # that does not even reach this far up or down the page was never part of this row's own grid
    # to begin with.
    row_column_clusters: List[List[Tuple[float, float, float]]] = []
    for wall in sorted(page_column_walls(lines), key=lambda wall: wall[0]):
        if row_column_clusters and wall[0] - row_column_clusters[-1][-1][0] <= PDF_TABLE_ROW_COLUMN_CLUSTER:
            row_column_clusters[-1].append(wall)
        else:
            row_column_clusters.append([wall])
    lines_by_y: Dict[float, List[Dict[str, Any]]] = {}
    for own_line in lines:
        lines_by_y.setdefault(round(own_line["y"], 1), []).append(own_line)

    def row_is_partial(y: float) -> bool:
        """Whether only a minority of the columns active at `y` have anything there - a table
        header cell wrapped to a second line only ever fills the few columns whose own header
        happened to wrap, never (close to) every column the way a genuine new row of the table
        does. See PDF_TABLE_ROW_MAX_FILL. Only meaningful once has_cell_border has already
        confirmed this is a real bordered table - page_column_walls is not reliable evidence of
        a column on its own for an informal, ruleless list (Powerupall page 85's own two-column
        checklist), where it missed a real column entirely and read a plain, complete row as a
        partial one.
        """
        active = [cluster for cluster in row_column_clusters
                 if any(wall[1] <= y <= wall[2] for wall in cluster)]
        if len(active) < 2:
            return False
        present = sum(1 for cluster in active
                      if any(abs(other["x"] - wall[0]) <= PDF_TABLE_ROW_COLUMN_CLUSTER
                            for wall in cluster
                            for other in lines_by_y.get(round(y, 1), [])))
        return present / len(active) <= PDF_TABLE_ROW_MAX_FILL

    def paragraph_column_x(paragraph: Dict[str, Any]) -> int:
        # The same reference paragraph_fits_line's own "left" uses: a paragraph's first line is
        # often indented or a list marker and does not sit at the column's real x, its second
        # line does. Keying last_by_x on whichever line was added most recently would otherwise
        # lose a paragraph the moment its own opening (indented) line is what got stored - and a
        # paragraph that has not grown a second line yet still needs to read as belonging to its
        # column for the justified_columns check below, or its own opening line blocks it.
        own = paragraph["lines"]
        x = round(own[1]["x"] if len(own) > 1 else own[0]["x"])
        if x in justified_columns:
            return x
        for column in justified_columns:
            if 0 < x - column <= PDF_LAYOUT_MAX_INDENT:
                return column
        return x

    paragraphs: List[Dict[str, Any]] = []
    # The paragraph most recently extended at each x, so a genuine paragraph split across an
    # interleaving column (see group_pdf_lines) can still be found once its own immediate
    # neighbour in the list turns out to belong to the other column. Guarded by the tighter
    # PDF_LAYOUT_CONTINUATION_MAX_RATIO, and by one of two further conditions, each covering a
    # different shape of interruption:
    #
    # - both x's are pdf_layout_justified_columns (real body text), for a paragraph already
    #   several lines long that another column interleaves with, line after line.
    # - the candidate's own column has_cell_border and the new line's row_is_partial, for a table
    #   header cell wrapped to a second line, interrupted once by the rest of its own row. Neither
    #   check alone is safe (measured against the full test corpus): a drawn border only proves
    #   "this is a table", not "this line is a wrap" (Table_Across_Pages' own row-to-row gap is
    #   every bit as tight as a wrapped cell's two lines), and row-completeness alone trusts
    #   page_column_walls to know every real column, which it does not for an informal, ruleless
    #   list (Powerupall page 85's two-column checklist). Required together, each rules out the
    #   other's failure case: a real table's rows fill (close to) every column of it, and an
    #   unruled list never has a border to begin with.
    last_by_x: Dict[int, Dict[str, Any]] = {}
    for line in lines:
        matched = None
        interrupting = paragraphs[-1] if paragraphs else None
        if interrupting is not None and paragraph_fits_line(interrupting, line):
            matched = interrupting
        else:
            candidate = last_by_x.get(round(line["x"]))
            if (candidate is not None and candidate is not interrupting
                    and interrupting is not None
                    and paragraph_fits_line(candidate, line,
                                            size_ratio=PDF_LAYOUT_CONTINUATION_MAX_RATIO)
                    and ((round(line["x"]) in justified_columns
                         and paragraph_column_x(interrupting) in justified_columns)
                         or (has_cell_border(candidate["lines"][0]["x"])
                             and row_is_partial(line["y"])))):
                matched = candidate
        if matched is not None:
            matched["lines"].append(line)
        else:
            matched = {"lines": [line]}
            paragraphs.append(matched)
        last_by_x[paragraph_column_x(matched)] = matched
    for paragraph in paragraphs:
        paragraph["text"] = " ".join(line["text"] for line in paragraph["lines"])
    return paragraphs


def page_column_walls(lines: List[Dict[str, Any]]) -> List[Tuple[float, float, float]]:
    """The x positions a table on this page sets its columns at, each with the band it spans.

    A cell whose neighbour happens to be empty on its own baselines has nothing beside it to
    measure against, and paragraph_line_limits then hands it the document's right margin: the
    long cell of MatterhornProtokoll's page 4 is 300pt wide and its translation was reflowed to
    550, straight across the two columns to its right.

    A column is an x that several lines start at and that hardly any line crosses - text set in
    one column runs over its own first-line indent all the time, a table's grid is not crossed at
    all. Crossings are counted inside the column's own band only, so the running footer of a
    landscape table does not disqualify it.

    The band handed back can reach further than the one the crossing check uses, though: a wall
    is proven by the lines that *start* a column, but a paragraph's last lines do not have to -
    the lowest line of Two_Column_Paper's left column sits 3pt below every line that begins at its
    wall, and a band built only from starting lines left it on the document's own right margin,
    straight across the column to its right. Extended one ordinary line-gap's worth of slack at a
    time through whatever text runs on continuously past the proven band, in either direction,
    which is why it stops at a real break - a wider gap than the page's own leading, such as the
    rule above a footnote - instead of reaching across the whole page the way a first attempt at
    this did (it walled a paragraph nowhere near it, see the test naming a picture in
    Stall-Kamera-System).
    """
    spacing = typical_line_spacing(lines)
    all_ys = sorted({line["y"] for line in lines}, reverse=True)
    walls = []
    for x in sorted({line["x"] for line in lines}):
        own = [line for line in lines if abs(line["x"] - x) <= 1.0]
        if len(own) < PDF_COLUMN_MIN_RUNS:
            continue
        bottom, top = min(line["y"] for line in own), max(line["y"] for line in own)
        crossings = sum(1 for line in lines
                        if line["x"] < x - 1.0 and line["right"] > x + 1.0
                        and bottom <= line["y"] <= top)
        if crossings >= len(own):
            continue
        if walls and x - walls[-1][0] <= 1.0:
            continue
        if spacing:
            tolerance = 2.0 * spacing
            for y in [y for y in all_ys if y < bottom]:
                if bottom - y > tolerance:
                    break
                bottom = y
            for y in reversed([y for y in all_ys if y > top]):
                if y - top > tolerance:
                    break
                top = y
        walls.append((x, bottom, top))
    # Two walls whose bands overlap are proven to run together over that stretch - a two-column
    # grid, not two unrelated columns that happen to share a page. Below that stretch, a wall
    # still applies even past its own last line: Two_Column_Paper's right column ends a dozen
    # lines above the left one, and paragraph_line_limits had nothing left to measure the left
    # column's remaining lines against, handing them the document's right margin straight across
    # the blank space where the right column used to be. Extended down to the lower of the two,
    # never up - the pair is only proven where they actually overlapped.
    for one in range(len(walls)):
        for other in range(len(walls)):
            if one == other:
                continue
            x1, bottom1, top1 = walls[one]
            x2, bottom2, top2 = walls[other]
            if bottom1 <= top2 and bottom2 <= top1 and bottom2 < bottom1:
                walls[one] = (x1, bottom2, top1)
    return walls


def pdf_page_widget_values(page) -> List[Dict[str, Any]]:
    """AcroForm field values on this page that carry text, each with its own annotation rect.

    A field's value is painted by the widget's own appearance stream, not the page's content
    stream: redact_translated_text cannot erase it, and a translation drawn as an ordinary
    overlay paragraph would sit invisibly underneath the widget's original value. Read apart from
    pdf_page_runs, whose text this filters back out (see its widget_rects), so the value can be
    written into the field itself instead - render_pdf_layout_overlay.
    """
    values = []
    for widget in page.widgets() or []:
        # Only a text field's value is language content. A checkbox or radio button's "value" is
        # one of its export states (often literally "Yes"/"Off"), not prose - writing a
        # translated string into one does not toggle it, it invalidates the state and the box
        # renders unchecked no matter what it held before.
        if widget.field_type != pymupdf.PDF_WIDGET_TYPE_TEXT:
            continue
        text = (widget.field_value or "").strip()
        if text:
            values.append({"text": text, "field_name": widget.field_name, "rect": widget.rect})
    return values


def extract_pdf_layout(content: bytes, page_range: str = "") -> List[Dict[str, Any]]:
    try:
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}") from exc
    try:
        if not document.page_count:
            raise HTTPException(status_code=422, detail="PDF has no pages")

        pages = []
        # Checked once on the first page that has text, exactly like extract_pdf_markdown_from_bytes:
        # a text layer is written by one producer for the whole file, and the check costs a render
        # plus an OSD call. Unlike the markdown path there is no per-page OCR fallback here, so
        # distrust aborts the whole layout pass instead of only that one page.
        trust_text_layer = None
        for index in parse_page_range(page_range, document.page_count):
            page = document[index - 1]
            # page.mediabox, not page.rect: pdf_page_runs/obstacles/rules extract into the page's own
            # raw, un-rotated frame (see pdf_page_flip_matrix), and the overlay is built and merged in
            # that same frame - /Rotate is left on the merged page for the viewer to apply once, to
            # both the kept original content and the overlay together.
            box = page.mediabox
            # ponytail: AcroForm widgets keep their original text on a rotated page - widget.rect's
            # own rotation convention isn't verified against pdf_page_flip_matrix yet, and a rotated
            # scan with a text field is a narrow case. Lift this once that's checked.
            rotated = page.rotation % 360 != 0
            obstacles = pdf_page_obstacles(page)
            rules = pdf_page_rules(page)
            row_rules = pdf_page_row_rules(page)
            widgets = [] if rotated else pdf_page_widget_values(page)
            lines = group_pdf_lines(
                pdf_page_runs(page, rules, [widget["rect"] for widget in widgets]),
                obstacles, rules)
            if trust_text_layer is None:
                page_text = page.get_text().strip()
                if page_text:
                    trust_text_layer = text_layer_is_trustworthy(content, index, page_text)
                    if not trust_text_layer:
                        raise HTTPException(
                            status_code=422,
                            detail="Text layer does not match the page's printed script. "
                                   "Falling back to OCR extraction.",
                        )
            pages.append({
                "number": index,
                "width": float(box.width),
                "height": float(box.height),
                "paragraphs": group_pdf_paragraphs(lines, rules),
                "obstacles": obstacles,
                "columns": page_column_walls(lines),
                "row_rules": row_rules,
                "widgets": widgets,
            })
        if not any(page["paragraphs"] or page["widgets"] for page in pages):
            raise HTTPException(
                status_code=422,
                detail="No positioned text found. This PDF may be scanned or image-only, "
                       "translate it with Plaintext enabled instead.",
            )
        return pages
    finally:
        document.close()


def wrap_text_to_width(text: str, width: Union[float, Sequence[float]], size: float,
                       bold: bool = False, serif: bool = False,
                       italic: bool = False) -> List[str]:
    """Break `text` into lines. `width` is one measure, or one per line for a block that is not
    a rectangle - a paragraph set around a picture, see paragraph_line_limits. The last entry
    carries on for any line past the end of the list."""
    widths = [float(width)] if isinstance(width, (int, float)) else [float(w) for w in width]

    # CJK text has no spaces between words, so any character is a valid break point; splitting
    # on whitespace there would treat the whole string as one unbreakable "word".
    cjk = detect_pdf_script(text) == "cjk"
    units = list(text) if cjk else text.split()
    separator = "" if cjk else " "

    lines: List[str] = []
    current = ""
    for unit in units:
        candidate = current + separator + unit if current else unit
        # Measured in the face the line will actually be drawn in: bold runs wider, so wrapping
        # it against regular metrics fits too much per line and overflows the column.
        if current and pdf_measure_text(candidate, size, bold, serif, italic) > widths[min(len(lines), len(widths) - 1)]:
            lines.append(current)
            current = unit
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


ROMAN_NUMERAL = re.compile(r"(?=[ivxlcdm])m*(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})\.?", re.IGNORECASE)


def has_translatable_text(text: str) -> bool:
    """Whether a paragraph holds anything a translator can work on.

    A fragment with no word in it - stray punctuation, the loose letters left over from a line the
    page edge cut through ("y g ;") - gives the model nothing to go on, and it answers by
    inventing: a row of dots, a "== Weblinks ==*". That invention is then laid out as if it were a
    translation and runs down the whole page. Such fragments keep their original instead.

    "A word" is a run of at least two letters, which also leaves dates and pure numbers alone. A
    roman numeral is excluded on top of that: front matter is paginated in them, and "iv" clears
    the two-letter bar while giving the model just as little to go on as "4" would - it came back
    as "iv iv iv iv iv" filling the bottom of the page. The odd real word that reads as a numeral
    ("mix", "did") is only affected standing alone as a whole paragraph.

    A bare URL is excluded for the same reason: it is not prose, the model rewrites it into
    something else entirely (one turned into "== Weblinks ==*"), and having no spaces it cannot
    be wrapped, so whatever comes back runs off the edge of the page.
    """
    stripped = LEADER_RUN.sub(" ", text).strip()
    if re.fullmatch(r"(https?://|www\.)\S+", stripped, re.IGNORECASE):
        return False
    if ROMAN_NUMERAL.fullmatch(stripped):
        return False
    # Two characters are a whole sentence in a script that writes without spaces, so the
    # two-letter bar has to count those characters and not the letters of a word.
    #
    # A single one is left alone, though, the same way a single Latin letter is. It carries as
    # little for the model to work from as "iv" does, and the fallback answers it with whatever
    # it likes: the closing line of Hoshi no Kagi is "— 完 —", "The End", and came back as
    # "wieso ist das alles?". Length cannot catch that afterwards - measured (Aug 2026) over 263
    # short pairs from four documents, the invented answer ran at 3.1 times its source while the
    # longest genuine one ran at 5.0 ("RAM" to "Arbeitsspeicher"), so any threshold that rejects
    # the one throws away the other. All genuine Japanese headings measured are three characters
    # or more and keep being translated.
    if len(CJK_DENSE_CHARS.findall(stripped)) > 1:
        return True
    return bool(re.search(r"[^\W\d_]{2,}", stripped))


def enclosing_box_bottom(paragraph: Dict[str, Any],
                         obstacles: Optional[List[Dict[str, float]]] = None,
                         row_rules: Optional[List[Dict[str, float]]] = None) -> Optional[float]:
    """The lower edge of the tightest shape the paragraph sits *inside*, or None.

    A table cell or a hint box. Growing past that edge is how translated text ended up outside
    the boxes it belongs to; unlike a paragraph below, this is a wall and not just a neighbour,
    which is what reflow_paragraph needs to know to shrink harder rather than overflow.
    """
    left = min(line["x"] for line in paragraph["lines"])
    right = max(line["right"] for line in paragraph["lines"])
    bottom = min(line["y"] for line in paragraph["lines"])
    size = max(line["size"] for line in paragraph["lines"])
    bottoms = [obstacle["bottom"] for obstacle in obstacles or []
               if obstacle["x"] <= left and obstacle["right"] >= right
               and obstacle["bottom"] < bottom and obstacle["top"] >= bottom - 0.5 * size]
    # A row rule drawn as a hairline never turns into an obstacle above - see pdf_page_row_rules -
    # but it is exactly as much a wall for the cell sitting on it. Matched the same way a column's
    # rule is in group_pdf_lines: the rule has to span the paragraph's own width, or it belongs to
    # a narrower cell beside it, not this one.
    bottoms += [rule["y"] for rule in row_rules or []
               if rule["left"] <= left and rule["right"] >= right and rule["y"] < bottom]
    return max(bottoms) if bottoms else None


def paragraph_floor(paragraph: Dict[str, Any], others: List[Dict[str, Any]],
                    obstacles: Optional[List[Dict[str, float]]] = None,
                    row_rules: Optional[List[Dict[str, float]]] = None) -> float:
    """How far down the paragraph may grow: the highest baseline below it in an overlapping
    column, or the bottom of the page when nothing stands in its way.

    Paragraphs beside it (table cells on the same row, a caption in the next column) must not
    limit it, or one long cell shrinks the whole row to nothing.

    An image or a filled shape below it stops it just as a paragraph does, and a box the
    paragraph sits *inside* stops it at its own lower edge - see pdf_page_obstacles.

    Falling back to the page edge rather than "unknown" is what keeps font sizes even: a
    paragraph with nothing below it has the whole rest of the page to overflow into, and
    shrinking it instead left the last paragraph of a column visibly smaller than the ones
    above it.
    """
    # ponytail: O(paragraphs^2) per page, fine at the few dozen a page holds; index by column if
    # a document ever shows up where it isn't.
    left = min(line["x"] for line in paragraph["lines"])
    right = max(line["right"] for line in paragraph["lines"])
    bottom = min(line["y"] for line in paragraph["lines"])
    floor = None
    for other in others:
        if other is paragraph:
            continue
        other_top = max(line["y"] for line in other["lines"])
        if other_top >= bottom:
            continue
        if max(line["right"] for line in other["lines"]) <= left:
            continue
        if min(line["x"] for line in other["lines"]) >= right:
            continue
        floor = other_top if floor is None else max(floor, other_top)
    size = max(line["size"] for line in paragraph["lines"])
    for obstacle in obstacles or []:
        if obstacle["right"] <= left or obstacle["x"] >= right:
            continue
        if obstacle["top"] < bottom - 0.5 * size:
            # Standing below the paragraph: its top edge is the limit. Half an em of clearance
            # keeps a shape that belongs to the last line itself - a highlight, a bullet icon -
            # from reading as something the paragraph has to stay above.
            floor = obstacle["top"] if floor is None else max(floor, obstacle["top"])
    # A box the paragraph sits inside stops it at its own lower edge, see enclosing_box_bottom.
    box = enclosing_box_bottom(paragraph, obstacles, row_rules)
    if box is not None:
        floor = box if floor is None else max(floor, box)
    return PDF_LAYOUT_EDGE_MARGIN if floor is None else max(floor, PDF_LAYOUT_EDGE_MARGIN)


def document_right_margin(pages: List[Dict[str, Any]]) -> float:
    """The right edge the document's own text stops at.

    Wrapping against the page edge instead let every paragraph run 30 to 50 points past the margin
    the original kept (measured: text ending at 542 on a 595pt page, reflowed to 575), which reads
    as the whole page having slid rightwards - the most visible flaw in the finished documents.

    The 95th percentile of the line ends, not the widest of them: one full-width header or rule
    sits at the page edge in an otherwise normally set document, and taking the maximum would hand
    its margin to every paragraph (Reddit_discussion.pdf: 538 at the 95th percentile, 596 widest).
    A paragraph whose own text runs past this keeps its own width, see paragraph_width_limit.
    """
    edge = min(page["width"] for page in pages) - PDF_LAYOUT_EDGE_MARGIN
    rights = sorted(line["right"] for page in pages
                    for paragraph in page["paragraphs"] for line in paragraph["lines"])
    return min(rights[int(0.95 * (len(rights) - 1))], edge) if rights else edge


def document_left_margin(pages: List[Dict[str, Any]]) -> float:
    """The left edge the document's own text starts at, the mirror of document_right_margin."""
    edge = PDF_LAYOUT_EDGE_MARGIN
    lefts = sorted(line["x"] for page in pages
                   for paragraph in page["paragraphs"] for line in paragraph["lines"])
    return max(lefts[int(0.05 * (len(lefts) - 1))], edge) if lefts else edge


def enclosing_box_sides(paragraph: Dict[str, Any],
                        obstacles: Optional[List[Dict[str, float]]] = None
                        ) -> Optional[Tuple[float, float]]:
    """The left and right edges of the tightest shape the paragraph sits inside, or None.

    The horizontal companion to enclosing_box_bottom, and selected the same way, so a paragraph
    in a table cell can be measured against that cell rather than against the whole page.
    """
    left = min(line["x"] for line in paragraph["lines"])
    right = max(line["right"] for line in paragraph["lines"])
    bottom = min(line["y"] for line in paragraph["lines"])
    top = max(line["y"] for line in paragraph["lines"])
    boxes = [obstacle for obstacle in obstacles or []
             if obstacle["x"] <= left and obstacle["right"] >= right
             and obstacle["bottom"] <= bottom and obstacle["top"] >= top]
    if not boxes:
        return None
    tightest = min(boxes, key=lambda obstacle: obstacle["right"] - obstacle["x"])
    return tightest["x"], tightest["right"]


def paragraph_center(paragraph: Dict[str, Any], left_margin: float, right_margin: float,
                     obstacles: Optional[List[Dict[str, float]]] = None) -> Optional[float]:
    """The centre a paragraph is set around, or None if it is set against its left edge.

    reflow_paragraph placed every line at the paragraph's left edge, so a centred heading came
    back flush left - by far the most frequent complaint about the finished documents, and the one
    the reader notices first because the title of the page moves.

    Every line has to stand clear of both margins by more than an ordinary indent and leave the
    same room on each side. Both halves are needed. Without the balance test an indented block
    counts as centred; without the clearance test so does ordinary flowed text, whose lines all
    fill the column and therefore all share its centre - that is how two justified list items of
    Powerupall ("5. The goal is to slowly become more conditioned...") first read as centred.

    Measured against the box the paragraph sits in where there is one, not the page: the "Minimum"
    column header of Systemrequirements is set against the left edge of its own cell and only
    happens to land near the middle of the page, which read as centred until the cell was used.
    """
    lines = paragraph["lines"]
    box = enclosing_box_sides(paragraph, obstacles)
    # Only a box narrower than the text column says anything about alignment. A full-page
    # background encloses every paragraph on the page, and measured against that the ordinary body
    # text of Geschäftsbedingungen sits symmetrically and read as centred.
    if box is not None and box[1] - box[0] < right_margin - left_margin:
        left_margin, right_margin = box
    tolerance = PDF_LAYOUT_CENTRE_TOLERANCE * max(right_margin - left_margin, 1.0)
    centres = []
    for line in lines:
        left_gap = line["x"] - left_margin
        right_gap = right_margin - line["right"]
        if min(left_gap, right_gap) <= PDF_LAYOUT_MAX_INDENT:
            return None
        if abs(left_gap - right_gap) > tolerance:
            return None
        centres.append((line["x"] + line["right"]) / 2)
    return sum(centres) / len(centres)


def paragraph_line_limits(
    paragraph: Dict[str, Any], others: List[Dict[str, Any]], right_margin: float,
    obstacles: Optional[List[Dict[str, float]]] = None,
    columns: Optional[Sequence[Tuple[float, float, float]]] = None,
) -> List[float]:
    """How far right each of the paragraph's lines may run, in absolute page coordinates.

    One limit per original line, because a paragraph is not always the rectangle a single width
    would make of it. Its own text ends where the *original* wording happened to end, which is
    not the width it had available - a heading alone on its line usually has most of the page to
    its right. The embedded font runs wider than the document's, so measuring against the
    original's ink makes almost every paragraph wrap one line early. The limit is whatever stands
    to the right of that line, or the document's own right margin.

    A line already running past that limit keeps its own width - a full-width heading in an
    otherwise narrower setting, see document_right_margin, or text overlapping a neighbour on the
    same baseline, which no measurement can tell from a column beside it. A box or an image is a
    wall and beats even that: an original overrunning its own table cell (Systemrequirements,
    "Graphics card") must not hand that overrun to the longer translation, which would then be
    reflowed clear across the next column.

    Per line rather than per paragraph because a picture rarely covers all of one. On page 1 of
    Stall-Kamera-System the image to the right reaches only the upper seven lines of a nine-line
    paragraph: those end at 215 and under, while the two lines below the image run on to 237. A
    single width took the widest of them, handed it to the lines beside the picture as well, and
    set them into it. The original was never rectangular there - it was set around the image.
    """
    own_left = min(item["x"] for item in paragraph["lines"])
    own_right = max(item["right"] for item in paragraph["lines"])
    # A shape only counts, as a wall or as something standing beside the paragraph, if it is big
    # enough to have held or blocked it. Below this it is an icon, a rule or a piece of
    # decoration that happens to sit near a line and says nothing about the room that line had.
    substantial = PDF_LAYOUT_WALL_MIN_SPAN * max(own_right - own_left, 1)

    limits = []
    # A column applying to one line of a paragraph almost always applies to the next: only the
    # lines that happened to *start* the column proved it, see page_column_walls, and its band can
    # end above a paragraph's last lines even though the column beside them is exactly the same
    # one. Carried forward within this paragraph only, and only across lines a column actually
    # reached - an obstacle clearing partway down a paragraph (Stall-Kamera-System) is a genuine
    # change of what stands beside it, not a gap in the evidence, and must not inherit anything.
    last_column_limit = None
    for line in paragraph["lines"]:
        limit = right_margin
        wall = None
        for other in others:
            if other is paragraph:
                continue
            for other_line in other["lines"]:
                if abs(other_line["y"] - line["y"]) > 0.6 * max(line["size"], other_line["size"]):
                    continue
                if other_line["x"] > line["x"]:
                    limit = min(limit, other_line["x"] - 2)
        # The next column of the page's own table, where the cell beside this line is empty and
        # there is no neighbouring text to measure against, see page_column_walls. Weaker evidence
        # than a drawn box, so it never cuts below the line's own ink - that is what the max below
        # leaves standing.
        column_limit = None
        for column, band_bottom, band_top in columns or []:
            if column > line["x"] + 1.0 and band_bottom <= line["y"] <= band_top:
                candidate = column - 2
                limit = min(limit, candidate)
                column_limit = candidate if column_limit is None else min(column_limit, candidate)
        if column_limit is not None:
            last_column_limit = column_limit
        elif last_column_limit is not None:
            limit = min(limit, last_column_limit)
        # An image beside the line stops it just as a neighbouring column does; a box the line
        # runs inside stops it at that box's own right edge, see pdf_page_obstacles.
        for obstacle in obstacles or []:
            if (obstacle["bottom"] >= line["y"] + 0.8 * line["size"]
                    or obstacle["top"] <= line["y"]):
                continue
            if (abs(obstacle["x"] - line["x"]) < 2
                    and abs(obstacle["right"] - line["right"]) < 2):
                # Drawn to the line's own measurements, so it is the line's own background and
                # not a box around it. Taken as a wall it wraps the paragraph to its shortest
                # line - one word per line, where that line was the word "or".
                continue
            if obstacle["x"] <= line["x"] < obstacle["right"]:
                # The box the line starts in, whether it ends inside that box or runs out of it.
                # Only this one counts as a wall: a shape merely standing to the right belongs to
                # whatever is beside the line, and a short line has all sorts of things to its
                # right that say nothing about the width the paragraph had.
                #
                # Stopped the same distance from the box's right edge as the paragraph starts from
                # its left one, rather than at a flat two points: the box on page 76 of Powerupall
                # insets its text by 10.5pt, and the translation filling it to within 2pt of the
                # frame read as text pressed against the right side of a box that has room on the
                # left.
                # Only where the paragraph really sits against this box's left edge. Past an
                # ordinary indent the two have nothing to do with each other: the "32 GB unified
                # memory" cell of Systemrequirements is also enclosed by the background of its
                # whole table row, which starts 307pt further left, and mirroring that (capped at
                # PDF_LAYOUT_MAX_INDENT) took 40pt off the cell's right edge - enough to wrap a
                # one-line cell into three and push them out under the row.
                gap = own_left - obstacle["x"]
                inset = gap if 2.0 <= gap <= PDF_LAYOUT_MAX_INDENT else 2.0
                edge = obstacle["right"] - inset
                wall = edge if wall is None else min(wall, edge)
            elif obstacle["x"] > line["x"] and obstacle["top"] - obstacle["bottom"] >= line["size"]:
                # Only a shape at least as tall as the line it is supposed to stop. Anything
                # flatter is an ornament standing near the text, not something set beside it: the
                # 5pt rule next to the word "or" on Systemrequirements would otherwise set that
                # whole paragraph one word per line. Height, not width - a picture narrower than
                # the paragraph still blocks it, which measuring against the paragraph's own width
                # got wrong, and let the translation back into the images of Stall-Kamera-System.
                # Half an em of air, not the two points a neighbouring line is given: text set
                # beside a picture keeps a visible gap to its frame, and at two points the body
                # text of Powerupall page 86 read as pressed against the picture beside it.
                limit = min(limit, obstacle["x"] - max(2.0, 0.5 * line["size"]))
        # This line's own ink, not the paragraph's widest: a line cannot be asked to wrap narrower
        # than the original already set it, but the fact that some *other* line of the paragraph
        # reaches further says nothing about the room this one had. Taking the whole paragraph's
        # width here is what handed the lines beside the Stall-Kamera image the measure of the two
        # lines below it.
        limit = max(limit, line["right"])
        # Get_Started_With_Smallpdf draws 139 icons and decorations, and any of them that happened
        # to start left of a line's own x claimed to be that line's box: a 42pt icon walled a 253pt
        # paragraph in at 19pt of width, which reflowed to one word per line and then blew past
        # PDF_LAYOUT_MAX_LINE_GROWTH, so three of the four body paragraphs kept their English.
        if wall is not None and wall - own_left >= substantial:
            limit = min(limit, wall)
        limits.append(limit)
    return limits


def paragraph_width_limit(
    paragraph: Dict[str, Any], others: List[Dict[str, Any]], right_margin: float,
    obstacles: Optional[List[Dict[str, float]]] = None,
) -> float:
    """The widest of paragraph_line_limits, for callers that want one number for the block."""
    return max(paragraph_line_limits(paragraph, others, right_margin, obstacles))


def paragraph_base_size(paragraph: Dict[str, Any]) -> float:
    """The size most of the paragraph's lines are set in.

    A paragraph can start with a larger heading line merged into smaller body lines (grouped for
    translation context, see group_pdf_paragraphs). Sizing the whole reflow off the maximum would
    blow the body text up to heading size.
    """
    return Counter(line["size"] for line in paragraph["lines"]).most_common(1)[0][0]


def paragraph_is_justified(paragraph: Dict[str, Any]) -> bool:
    """Whether the original set this paragraph flush on both edges.

    Three lines at least: two that happen to end together say nothing, a whole column of them is
    the setting. The last line of a paragraph is short by nature and never counts.

    Four points of play, because a flush edge is only flush to the eye - the last glyph of a line
    carries its own side bearing, and a line ending in "s" stops a little short of one ending in a
    full stop. Measured over the test documents, that is also where the count settles: of the 408
    paragraphs in the book 277 pass at 2 points and 391 at 4, and widening to 6, 8 or 12 adds three
    more in total, while the ragged documents stay at nought to two throughout.
    """
    lines = paragraph["lines"]
    if len(lines) < 3:
        return False
    edges = [line["right"] for line in lines[:-1]]
    return max(edges) - min(edges) <= 4.0


def reflow_paragraph(
    paragraph: Dict[str, Any],
    text: str,
    floor: Optional[float] = None,
    width_limit: Optional[Union[float, Sequence[float]]] = None,
    scale: Optional[float] = None,
    box_floor: Optional[float] = None,
    centre: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Lay the translation out in the column the paragraph's original lines occupied.

    Returns the absolutely positioned text lines. Clearing the original text is not this
    function's job: render_pdf_layout_overlay redacts it out of the source page first.

    `floor` is the baseline of whatever sits directly below in the same column: a translation
    longer than its original keeps running past the last line, and without that limit it runs
    straight into the next paragraph. Passing None keeps the old unbounded behaviour.

    `scale` sets the font size outright, as a fraction of the paragraph's own base size, instead
    of looking for the largest one that fits. Its caller uses that to give every paragraph on a
    page the same size, see render_pdf_layout_overlay.

    `box_floor` is the lower edge of the box the paragraph sits inside, where there is one (see
    enclosing_box_bottom). A wall, not a neighbour: an extra line there does not crowd the next
    paragraph, it stands outside the table cell, so it is worth shrinking harder to avoid.

    `centre` is the axis a centred paragraph is set around, see paragraph_center. Passing None
    sets the paragraph against its left edge, which is what everything else on a page wants.
    """
    lines = paragraph["lines"]
    left = min(line["x"] for line in lines)
    # One right edge per line, so a paragraph the original set around a picture is set around it
    # again, see paragraph_line_limits. A single number still works and applies to every line.
    if width_limit is None:
        rights = [max(line["right"] for line in lines)]
    elif isinstance(width_limit, (int, float)):
        rights = [float(width_limit)]
    else:
        rights = [float(edge) for edge in width_limit] or [max(line["right"] for line in lines)]

    def right_at(index: int) -> float:
        # Past the original's last line there is nothing left to measure against, so the overflow
        # carries on at the last known edge. What stands *below* the paragraph is paragraph_floor's
        # business, not this one's.
        return rights[min(index, len(rights) - 1)]

    right = max(rights)
    widths = [max(edge - left, 10.0) for edge in rights]
    base_size = paragraph_base_size(paragraph)
    # Same reasoning as base_size: a paragraph is drawn in whichever face most of its lines use,
    # so a bold heading stays bold instead of flattening to regular body text.
    bold = sum(1 for line in lines if line.get("bold")) * 2 > len(lines)
    serif = sum(1 for line in lines if line.get("serif")) * 2 > len(lines)
    italic = sum(1 for line in lines if line.get("italic")) * 2 > len(lines)
    color = Counter(line.get("color", 0) for line in lines).most_common(1)[0][0]
    if len(lines) > 1:
        leading = (lines[0]["y"] - lines[-1]["y"]) / (len(lines) - 1)
    else:
        leading = 1.2 * base_size

    def overflow_leading(size: float) -> float:
        # The paragraph's own leading, whatever size it ended up being set in. Its first lines
        # keep the original's baselines, so anything else leaves the last line sitting at a
        # different distance than every line above it: scaled in proportion to the shrinking, a
        # paragraph set at 0.77 on Powerupall p. 8 ran at 15.0pt throughout and closed at 11.6.
        # Spacing overflow lines this far down is what used to push them into the next paragraph,
        # which is no longer the trade it was - a crowded paragraph now shrinks instead, see
        # PDF_LAYOUT_CROWDED_MIN_SCALE.
        spacing = leading
        if boxed:
            # Nothing below to run into but the wall itself, so an overflow line is set as tight
            # as it can be read: every point saved is a point less of it standing outside.
            spacing = min(spacing, 1.05 * size)
        return spacing

    def fits(count: int, size: float) -> bool:
        if count <= len(lines):
            return True
        if floor is None:
            # Nothing known to be below: keeping the translation inside the original's own lines
            # is the only bound there is.
            return False
        lowest = lines[-1]["y"] - overflow_leading(size) * (count - len(lines))
        # A line occupies roughly a quarter em below its baseline and nearly a full em above, so
        # the lowest overflow baseline has to clear the next paragraph's baseline by that much.
        # Against a box's lower edge only the descenders have to stay above it - keeping the full
        # em there costs a roomy cell a line, and with it type size, for nothing.
        # 1.15 only keeps ascenders/descenders from touching - enough to not overlap, not enough to
        # read as a paragraph break. Measured on Landscape_Mixed_Pages page 3: the original sets
        # paragraphs 21.5pt apart at 10pt body text, 2.15x the font size, against 1.35x for an
        # ordinary line of the same paragraph - a real paragraph gap is close to double an ordinary
        # line's leading, not merely clear of it.
        clearance = 0.3 if floor == box_floor else 2.0
        return lowest >= floor + clearance * size

    # Walled in: inside a box that has no room left for another line. A third of an em of
    # clearance, not the full 1.15 fits() keeps against a baseline - a box's lower edge is an
    # edge, and only the descenders of the last line have to stay above it. A page-sized
    # background rectangle encloses a paragraph too, and fails this test by a wide margin.
    boxed = (box_floor is not None
             and lines[-1]["y"] - leading < box_floor + 0.3 * base_size)

    # The original's own first-line indent, kept: the paragraphs of a page like this are set
    # without a blank line between them, so with the indent gone there is nothing left to show
    # where one ends. Right-to-left text hangs off the other edge and is left alone.
    indent = lines[0]["x"] - left if len(lines) > 1 else 0.0
    if not 0 < indent <= PDF_LAYOUT_MAX_INDENT or is_rtl_text(text):
        indent = 0.0

    def wrap(size: float) -> List[str]:
        if not indent:
            return wrap_text_to_width(text, widths, size, bold, serif, italic)
        first = wrap_text_to_width(text, widths[0] - indent, size, bold, serif, italic)[0]
        # Only when the wrap really is the text with a break put in it, which is every script
        # that separates words; CJK is wrapped character by character and joined differently.
        if not text.startswith(first):
            return wrap_text_to_width(text, widths, size, bold, serif, italic)
        rest = text[len(first):].strip()
        # The remainder starts on the second line, so it is measured against the second edge on.
        return [first] + (wrap_text_to_width(rest, widths[1:] or widths, size, bold, serif, italic)
                          if rest else [])

    size = base_size if scale is None else base_size * scale
    wrapped = wrap(size)
    # The embedded substitute font runs wider than most fonts documents are set in, so text that
    # filled n lines in the original spills into n+1 here - measured across the test documents,
    # 13 of 21 paragraphs needed an extra line for *identical* text, and not one of them needed
    # more than 10% off to fit again. Tighten by up to that before accepting the extra line: a
    # slightly smaller line reads better than a paragraph that grew one.
    tighten_to = base_size * (PDF_LAYOUT_BOXED_MIN_SCALE if boxed else PDF_LAYOUT_TIGHTEN_SCALE)
    while scale is None and len(wrapped) > len(lines) and size > tighten_to:
        size = max(size * 0.98, tighten_to)
        wrapped = wrap(size)
    # A paragraph with a neighbour directly below it may shrink past the ordinary minimum rather
    # than run into it. Overlapping text cannot be read at all, small text only reads small, so
    # the trade is worth making - but only for the paragraph that needs it, which is why the
    # document-wide scale in render_pdf_layout_overlay stays clamped at the ordinary minimum.
    # Measured (Aug 2026) on the Powerupall pages reported as colliding: four paragraphs across
    # pages 15, 50 and 92 still ran into the next one at 0.70 and needed 0.54 to 0.64.
    #
    # Not for a CJK source, though: a German translation of Japanese or Chinese routinely needs
    # several times the line count the dense source packed into the same width, and shrinking
    # *that* down to 0.5 to avoid overflow reads as a typo, not a translation - Hoshi no Kagi came
    # back with two paragraphs at 0.51 next to the rest of the page at 0.73. There the trade runs
    # the other way: overflowing a couple of lines past the neighbour below is the smaller fault.
    source_text = " ".join(line["text"] for line in lines)
    crowds = floor is not None and not boxed and detect_pdf_script(source_text) != "cjk"
    lowest = base_size * (PDF_LAYOUT_CROWDED_MIN_SCALE if crowds else PDF_LAYOUT_MIN_SCALE)
    while scale is None and not fits(len(wrapped), size) and size > lowest:
        size = max(size * 0.95, lowest)
        wrapped = wrap(size)

    # A translation into Hebrew or Arabic hangs off the right edge of the column, the way the
    # column would have been set had the document been written in that language.
    rtl = is_rtl_text(text)
    # Set flush on both edges where the original was, against the same edge the text was wrapped
    # to. Not against the original's own right edge: paragraph_width_limit sits at or past it, and
    # wrapping to the narrower one would cost lines and with them font size.
    # A centred paragraph is set around its axis instead, and never justified: stretching a centred
    # heading to a flush right edge would undo the centring line by line.
    justify = paragraph_is_justified(paragraph) and not rtl and centre is None
    placed = []
    for index, wrapped_line in enumerate(wrapped):
        # Translations longer than the original keep running below the last line: overflowing
        # is recoverable for the reader, silently cut off text is not. At PDF_LAYOUT_MIN_SCALE
        # even a floor cannot always be honoured, and then it still overflows rather than losing
        # the tail of the sentence.
        y = (
            lines[index]["y"] if index < len(lines)
            else lines[-1]["y"] - overflow_leading(size) * (index - len(lines) + 1)
        )
        placed.append({
            "text": wrapped_line,
            "font": {value: name for name, value in PDF_FONT_FACES.items()}[(bold, serif, italic)],
            "size": size,
            "line_height": 0,
            "x": (right_at(index) - pdf_measure_text(wrapped_line, size, bold, serif, italic) if rtl
                  else centre - pdf_measure_text(wrapped_line, size, bold, serif, italic) / 2
                  if centre is not None
                  else left + (indent if index == 0 else 0.0)),
            "y": y,
            "color": color,
        })
    if justify:
        for index, line in enumerate(placed[:-1]):
            # Each line to its own edge: a paragraph set around a picture is flush against the
            # picture where it passes it and against the margin below, not against one measure.
            line["justify_to"] = right_at(index)
    return placed


def redact_translated_text(content: bytes, pages: List[Dict[str, Any]], translations: List[str],
                           widget_updates: Optional[Dict[Tuple[int, str], str]] = None) -> bytes:
    """Delete the original text of every translated paragraph from the source PDF.

    Painting boxes over it (the previous approach) left it in the file: copy/paste and Ctrl+F on
    a "translated" document still returned the original wording. MuPDF's redaction removes the
    text operators themselves, and with fill off and images/line art excluded it leaves the page
    background, table shading, icons and vector graphics untouched.
    """
    with pymupdf.open(stream=content, filetype="pdf") as document:
        index = 0
        for page_data in pages:
            page = document[page_data["number"] - 1]
            redacted = False
            # Every line of the page, to keep a rectangle out of the cell beside it: the point of
            # padding either side is to catch the glyph's own overhang, and in a table that point
            # lands in the neighbouring cell. On page 13 of MatterhornProtokoll the "Software" cell
            # ends 0.2pt before "21-001" begins, and the padded rectangle took the "2" with it.
            neighbours = [line for other in page_data["paragraphs"] for line in other["lines"]]
            for paragraph in page_data["paragraphs"]:
                # A paragraph without a translation keeps its original text rather than being
                # erased with nothing put in its place. An untranslatable fragment (a table's numeric
                # column, say) is redacted like any other: render_pdf_layout_overlay draws its own
                # original text back at whatever size level_table_sizes settled on, and skipping the
                # redaction here would leave that redraw stamped straight over the untouched original.
                if index < len(translations) and translations[index].strip():
                    for line in paragraph["lines"]:
                        size = line["size"]
                        # Still narrower than the line's full height - a rectangle only has to touch
                        # a glyph for MuPDF to drop it, and one tall enough to hold ascenders would
                        # reach into the line above. It does reach below the baseline far enough to
                        # cover the link underlines that sit there (measured at 0.23em), which have
                        # to go with the text they underline or they end up striking through an
                        # unrelated part of the translation.
                        on_baseline = [other for other in neighbours
                                       if other is not line
                                       and abs(other["y"] - line["y"]) <= 0.3 * size]
                        left = max([other["right"] for other in on_baseline
                                    if other["right"] <= line["x"]] + [line["x"] - 1])
                        right = min([other["x"] for other in on_baseline
                                     if other["x"] >= line["right"]] + [line["right"] + 1])
                        rectangle = pymupdf.Rect(
                            left,
                            line["y"] - 0.30 * size,
                            right,
                            line["y"] + 0.5 * size,
                        ) * pdf_page_flip_matrix(page)
                        page.add_redact_annot(rectangle, fill=False)
                        redacted = True
                index += 1
            # Not redacted: a field's value never was page content to begin with, see
            # pdf_page_runs' widget_rects. It skips the same number of translations as
            # render_pdf_layout_overlay assigned it, though, to stay on the same paragraph past it.
            index += len(page_data.get("widgets") or [])
            if redacted:
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    # IF_COVERED, not NONE or IF_TOUCHED: an underline lies wholly inside its line's
                    # rectangle and goes, while page backgrounds, table shading and rules extend past
                    # it and stay. IF_TOUCHED would strip every panel a line of text sits on.
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE,
                )
            for widget in page.widgets() or []:
                value = (widget_updates or {}).get((page_data["number"], widget.field_name))
                if value is not None:
                    widget.field_value = value
                    widget.update()
        return document.tobytes(garbage=3, deflate=True)


def level_table_sizes(paragraphs: List[Dict[str, Any]], bases: List[float], targets: List[float],
                      obstacles: List[Dict[str, float]]):
    """Pull every paragraph of a table down to the smallest size any of them needs, in place.

    Deliberately unlike the original: at the scale a document ends up in nobody can tell 11pt from
    12pt any more, all that is left is the impression of several sizes in one table, and side by
    side that shows (Powerupall page 50, "Kreative und energetische / Teilnahme an Elternschaft",
    11.0 against 12.0, rendered 7.5 against 6.6; page 72 alternated 6.6 and 7.7 row by row).

    Three relations put paragraphs into the same group, and only ever between paragraphs whose own
    sizes are close - the same 20 % group_pdf_paragraphs takes for a change of size, so 11 against
    12 is levelled and a 16pt header cell against 11pt body keeps its size:

    - beside each other, which is what makes a row. Not "on one baseline": cells of a row start
      within a line of each other rather than on the same line, those two 6pt apart.
    - inside the same drawn cell, which is what makes a cell hold one size even where its text
      falls into several paragraphs.
    - the same left edge one above the other, which joins the rows of a table into the table. Only
      between paragraphs that are cells to begin with, so ordinary body text is never caught.
    """
    # ponytail: O(n²) pairwise comparison over a page's paragraphs, switch to a spatial index if a
    # document's tables ever make this the bottleneck.
    count = len(paragraphs)
    extent = [(min(line["x"] for line in item["lines"]),
               max(line["right"] for line in item["lines"]),
               min(line["y"] for line in item["lines"]),
               max(line["y"] for line in item["lines"])) for item in paragraphs]
    cell = [(enclosing_box_sides(item, obstacles), enclosing_box_bottom(item, obstacles))
            for item in paragraphs]
    # The third relation below is only supposed to catch cells, but nothing in its own geometry
    # test tells a table column from an ordinary column of running text - both are paragraphs
    # stacked at the same left edge, and Two_Column_Paper's body column gaps (as tight as 1.2x
    # the font size in its own tighter typesetting) are well inside the 3x this allows just as a
    # real table row's is. Same signal already used to tell the two apart in group_pdf_paragraphs:
    # a table cell's text is ragged-right, a real column fills to a consistent right edge.
    justified_columns = pdf_layout_justified_columns(
        [line for item in paragraphs for line in item["lines"]])
    group = list(range(count))

    def root(index):
        while group[index] != index:
            group[index] = group[group[index]]
            index = group[index]
        return index

    def join(one, other):
        group[root(one)] = root(other)

    def comparable(one, other):
        return abs(bases[one] - bases[other]) <= 0.2 * max(bases[one], bases[other])

    beside = [False] * count
    for index in range(count):
        left, right, bottom, top = extent[index]
        for other in range(index + 1, count):
            other_left, other_right, other_bottom, other_top = extent[other]
            # Clear of each other horizontally and overlapping vertically, with a line of
            # tolerance: paragraphs of one column always overlap horizontally and never pair up.
            if (other_left > right or other_right < left) \
                    and other_bottom - bases[index] <= top \
                    and other_top + bases[index] >= bottom:
                beside[index] = beside[other] = True
                # Two columns of running text land in this same "beside" test whenever a pair of
                # paragraphs happens to occupy close to the same vertical range - ordinary for two
                # columns of similar length, and Two_Column_Paper's own body columns joined this
                # way and pulled the whole page down to 3.8pt. A real table row's cells are never
                # a justified column themselves, see the vertical-stacking relation below.
                if (comparable(index, other) and round(left) not in justified_columns
                        and round(other_left) not in justified_columns):
                    join(index, other)
            # The same cell, where the document draws one.
            elif cell[index][0] is not None and cell[index] == cell[other] and comparable(index,
                                                                                         other):
                join(index, other)

    for index in range(count):
        for other in range(index + 1, count):
            # One above the other in the same column of the table, which is what holds its rows
            # together: the rows themselves never overlap vertically and cannot be joined by the
            # test above.
            if beside[index] and beside[other] and comparable(index, other) \
                    and abs(extent[index][0] - extent[other][0]) <= 3 \
                    and round(extent[index][0]) not in justified_columns \
                    and min(extent[index][2] - extent[other][3],
                            extent[other][2] - extent[index][3]) <= 3 * bases[index]:
                join(index, other)

    smallest: Dict[int, float] = {}
    members: Dict[int, int] = {}
    for index in range(count):
        key = root(index)
        smallest[key] = min(smallest.get(key, targets[index]), targets[index])
        members[key] = members.get(key, 0) + 1
    for index in range(count):
        key = root(index)
        level = smallest[key]
        # A single cramped cell is free to be that small on its own - the floor only stops it
        # pulling every *other* cell of a table it shares no size problem with down with it. A
        # wide table (Landscape_Mixed_Pages, 10 columns by 9 rows) chains every cell into one
        # group through its rows and columns, so one long word in one narrow, crowded cell used to
        # take the whole table from 8.5pt to a uniform, illegible 4.2pt.
        if members[key] > 1:
            level = max(level, min(PDF_LAYOUT_ABSOLUTE_MIN_SIZE, bases[index]))
        targets[index] = level


def render_pdf_layout_overlay(content: bytes, pages: List[Dict[str, Any]], translations: List[str]) -> bytes:
    """Stamp the translated text onto the original pages, so images, icons and vector graphics
    survive untouched."""
    overlay_pages = []
    right_margin = document_right_margin(pages)
    left_margin = document_left_margin(pages)
    # Paragraphs whose reflow is rejected below have to keep their original text, which means the
    # redaction pass must not erase them either - it is driven off the same list.
    kept = list(translations)
    index = 0
    # (paragraph, translation, floor, width limit, box floor, centre, the lines it laid out),
    # per page. Collected for the whole document before anything is drawn, because the size every
    # paragraph ends up in is a decision about the document and not about its page, see below.
    per_page: List[List[Tuple[Any, ...]]] = []
    # The smallest scale any paragraph of a given base size needed, anywhere in the document.
    scales: Dict[float, float] = {}
    # The same for the paragraphs that had to shrink past PDF_LAYOUT_MIN_SCALE to clear what sits
    # below them, kept per page rather than per document, see the second pass.
    cramped: Dict[float, float] = {}
    cramped_pages: List[Dict[float, float]] = []
    # AcroForm fields never go through reflow - a field holds one value, not lines to wrap - so
    # they are collected separately here and written into the field itself by
    # redact_translated_text, but still consume translations in lockstep with everything else:
    # each page's widgets sit right after its paragraphs in the same flat, position-matched list.
    widget_updates: Dict[Tuple[int, str], str] = {}
    for page in pages:
        reflowed: List[Tuple[Any, ...]] = []
        for paragraph in page["paragraphs"]:
            if index < len(translations) and translations[index].strip():
                # A fragment too short/numeric to translate (has_translatable_text) still keeps
                # its own original text - translations[index] may be the model inventing on it,
                # a page number or a table cell like "4.8" is exactly what guard_hallucination's
                # length check cannot catch - but it still needs to go through reflow and join
                # level_table_sizes' grouping below: a table's numeric column left untouched
                # while its header and label columns shrink to fit their translation looks just
                # as inconsistent as the header staying split from the column ever did.
                translatable = has_translatable_text(paragraph["text"])
                text = translations[index] if translatable else paragraph["text"]
                obstacles = page.get("obstacles") or []
                row_rules = page.get("row_rules") or []
                floor = paragraph_floor(paragraph, page["paragraphs"], obstacles, row_rules)
                box_floor = enclosing_box_bottom(paragraph, obstacles, row_rules)
                width_limit = paragraph_line_limits(
                    paragraph, page["paragraphs"], right_margin, obstacles,
                    page.get("columns"))
                centre = paragraph_center(paragraph, left_margin, right_margin, obstacles)
                placed = reflow_paragraph(paragraph, text, floor, width_limit,
                                          box_floor=box_floor, centre=centre)
                if len(placed) > PDF_LAYOUT_MAX_LINE_GROWTH * len(paragraph["lines"]):
                    # Not a translation of this paragraph any more. Overflow is tolerated, but a
                    # block several times its original height buries whatever sits below it, and
                    # the original is the better of the two things to be looking at.
                    kept[index] = ""
                else:
                    reflowed.append((paragraph, text, floor, width_limit,
                                     box_floor, centre, placed))
                    base = paragraph_base_size(paragraph)
                    key = round(base, 1)
                    # Clamped at the ordinary minimum: a paragraph that had to go below it is
                    # walled in by its own box (see PDF_LAYOUT_BOXED_MIN_SCALE), and one cramped
                    # table cell must not set the size of every paragraph in the document.
                    scales[key] = min(scales.get(key, 1.0),
                                      max(placed[0]["size"] / base, PDF_LAYOUT_MIN_SCALE))
                    # And, separately, the smallest scale the paragraphs that had to go below that
                    # minimum needed on this page, see the second pass.
                    if placed[0]["size"] < base * PDF_LAYOUT_MIN_SCALE:
                        cramped[key] = min(cramped.get(key, 1.0), placed[0]["size"] / base)
            index += 1
        for widget in page.get("widgets") or []:
            if (index < len(translations) and translations[index].strip()
                    and has_translatable_text(widget["text"])):
                widget_updates[(page["number"], widget["field_name"])] = translations[index].strip()
            index += 1
        per_page.append(reflowed)
        cramped_pages.append(cramped)
        cramped = {}

    # Every paragraph the document sets in one size is redrawn in one size. Each shrinks itself
    # just enough to fit its own translation, which left body text at 0.7 next to body text at 1.0
    # - first in the same column, and once that was settled per page, still from one page to the
    # next: measured over the 92 text pages of Powerupall, body text set at 11.0 throughout came
    # back between 7.7 and 11.0, with a different size on facing pages. Headings keep their own
    # scale, they are a size of their own to begin with.
    #
    # The smallest scale the document needs, not an average of them: it is the only one every
    # paragraph still fits in, and anything larger buys its evenness by pushing the densest pages
    # into overflow, which is the more visible fault of the two.
    # A paragraph too cramped for even that scale shrinks further on its own (see
    # PDF_LAYOUT_CROWDED_MIN_SCALE), and side by side those came out at six different sizes across
    # one row of the research table on page 50 of Powerupall. They are levelled with each other per
    # page: the ones that had to go below the document's scale all take the smallest of them, while
    # the ordinary paragraphs around them keep it. Per page and not per document, because one
    # cramped table must not take the size of every cramped paragraph in the book with it.
    for page, reflowed, cramped in zip(pages, per_page, cramped_pages):
        bases = [paragraph_base_size(item[0]) for item in reflowed]
        targets = []
        for (paragraph, _, _, _, _, _, placed), base in zip(reflowed, bases):
            scale = scales[round(base, 1)]
            if placed[0]["size"] < base * scale:
                scale = cramped.get(round(base, 1), placed[0]["size"] / base)
            # Never larger than what this paragraph found on its own, so it still fits.
            targets.append(min(placed[0]["size"], base * scale))
        level_table_sizes([item[0] for item in reflowed], bases, targets,
                          page.get("obstacles") or [])

        lines: List[Dict[str, Any]] = []
        for (paragraph, text, floor, width_limit, box_floor, centre, placed), base, target in zip(
                reflowed, bases, targets):
            # Not just shrinking: level_table_sizes' floor can also raise a target back above a
            # single cramped cell's own tiny natural size, and that cell has to be redrawn at the
            # bigger size to actually show it - a bare "> target" only ever pulled sizes down.
            if abs(placed[0]["size"] - target) > 0.05:
                placed = reflow_paragraph(paragraph, text, floor, width_limit, target / base,
                                          box_floor=box_floor, centre=centre)
            lines.extend(placed)

        overlay_pages.append({
            "width": page["width"],
            "height": page["height"],
            "margin": 0,
            "source_page": "",
            "continuation": False,
            "footer": False,
            "lines": lines,
        })

    overlay_reader = PdfReader(BytesIO(create_pdf_from_pages(overlay_pages)))
    reader = PdfReader(BytesIO(redact_translated_text(content, pages, kept, widget_updates)))
    writer = PdfWriter()
    # Only the selected pages are translated, so only those are exported. Keeping the untouched
    # rest would make a single-page selection look like the unconverted original.
    for position, page in enumerate(pages):
        original = reader.pages[page["number"] - 1]
        original.merge_page(overlay_reader.pages[position])
        writer.add_page(original)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def export_pdf_layout_with_translated_text(content: bytes, translated_text: str, page_range: str = "") -> bytes:
    # Split without dropping empty blocks: translations are matched to paragraphs by position.
    blocks = [block.strip() for block in translated_text.split("\n\n")]
    return render_pdf_layout_overlay(content, extract_pdf_layout(content, page_range), blocks)


def exception_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def docx_paragraph_page_numbers(paragraphs, w_ns: str) -> List[int]:
    """Page number (1-based) each word/document.xml body paragraph falls on, counting only manual
    page breaks (<w:br w:type="page"/>) - the only page boundary actually recorded in the file.
    A page's natural, layout-driven breaks exist only once Word or a printer renders the document
    with a particular page size, margins and fonts, none of which the file itself commits to.
    """
    br_tag, type_attr = f"{{{w_ns}}}br", f"{{{w_ns}}}type"
    pages = []
    page = 1
    for paragraph in paragraphs:
        pages.append(page)
        page += sum(1 for br in paragraph.iter(br_tag) if br.get(type_attr) == "page")
    return pages


def docx_paragraphs_in_range(root, w_ns: str, namespaces: Dict[str, str], page_range: str):
    """Body paragraphs of word/document.xml, filtered to page_range's manual-break pages. Returns
    every paragraph unfiltered when page_range is empty or this isn't the main document part -
    headers, footers, footnotes and comments repeat across pages rather than belonging to one."""
    paragraphs = root.findall(".//w:p", namespaces)
    if not page_range.strip():
        return paragraphs
    page_numbers = docx_paragraph_page_numbers(paragraphs, w_ns)
    total_pages = page_numbers[-1] if page_numbers else 1
    selected = set(parse_page_range(page_range, total_pages))
    return [paragraph for paragraph, page in zip(paragraphs, page_numbers) if page in selected]


def extract_docx_text_from_bytes(content: bytes, page_range: str = "") -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as docx:
            ensure_zip_size(docx)
            parts = docx_text_part_names(docx)
            documents = {part: docx.read(part) for part in parts}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read DOCX: {exc}") from exc

    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    namespaces = {"w": w_ns}

    paragraphs = []
    for part, document in documents.items():
        try:
            root = ElementTree.fromstring(document)
        except ElementTree.ParseError as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse DOCX XML {part}: {exc}") from exc
        body_paragraphs = docx_paragraphs_in_range(
            root, w_ns, namespaces, page_range if part == "word/document.xml" else "")
        for paragraph in body_paragraphs:
            for segment in docx_paragraph_run_segments(paragraph, w_ns):
                text = re.sub(r"\s+", " ", "".join(node.text or "" for node in segment)).strip()
                if text:
                    paragraphs.append(text)

    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in DOCX")
    return result


def docx_run_entries(paragraph, w_ns: str):
    """Yield (node, signature) for each <w:t>/<w:tab>/<w:br>/<w:cr> in a paragraph, in document
    order. signature is (bold, italic, is_hyperlink) for a text node, or "BREAK" for a tab/break.

    Recurses into every descendant rather than assuming runs sit directly under <w:p> or
    <w:hyperlink> - Word wraps runs in other elements too (tracked-changes' <w:ins>/<w:del>,
    <w:smartTag>, ...), and a plain child-only walk would silently skip their text. A DOCX
    hyperlink is a separate <w:hyperlink> element wrapping its run(s) rather than a flag on the
    run's own properties (contrast PPTX's <a:hlinkClick> inside <a:rPr>), so is_hyperlink is
    carried down from the moment a <w:hyperlink> ancestor is entered.
    """
    r_tag, rpr_tag = f"{{{w_ns}}}r", f"{{{w_ns}}}rPr"
    hyperlink_tag = f"{{{w_ns}}}hyperlink"
    text_tag = f"{{{w_ns}}}t"
    space_tags = {f"{{{w_ns}}}tab", f"{{{w_ns}}}br", f"{{{w_ns}}}cr"}
    bold_tag, italic_tag = f"{{{w_ns}}}b", f"{{{w_ns}}}i"

    def walk(element, is_hyperlink, signature):
        if element.tag == hyperlink_tag:
            is_hyperlink = True
        if element.tag == r_tag:
            rpr = element.find(rpr_tag)
            signature = (
                rpr is not None and rpr.find(bold_tag) is not None,
                rpr is not None and rpr.find(italic_tag) is not None,
                is_hyperlink,
            )
        if element.tag == text_tag:
            yield element, signature
        elif element.tag in space_tags:
            yield element, "BREAK"
        for child in element:
            yield from walk(child, is_hyperlink, signature)

    yield from walk(paragraph, False, (False, False, False))


def docx_paragraph_run_segments(paragraph, w_ns: str) -> List[List[Any]]:
    """Group a paragraph's <w:t> nodes into segments split at tab/line-break marks and wherever
    the run's bold/italic/hyperlink signature changes with real content on both sides.

    A tab-aligned "label<tab>value" line (a resume's "10.2022 bis heute<tab>Stellwerker bei
    ...") needs its two sides translated, and reinjected, independently - treating the whole
    line as one translatable block put the entire translation into the label's run and blanked
    the value's, so the original tab stops jumped to nothing. A run of different formatting
    mid-sentence is the same problem without a tab: a hyperlink run translated as part of the
    surrounding sentence and reinjected into the first run left the hyperlink with no visible,
    clickable text at all. A formatting change on a run that is still empty (leading padding,
    e.g.) does not start a new segment - the padding stays wherever the content after it lands.
    """
    segments: List[List[Any]] = []
    current: List[Any] = []
    current_signature = None
    for node, signature in docx_run_entries(paragraph, w_ns):
        has_content = any((n.text or "").strip() for n in current)
        if signature == "BREAK":
            if has_content:
                segments.append(current)
                current = []
                current_signature = None
            continue
        if has_content and signature != current_signature:
            segments.append(current)
            current = []
        current.append(node)
        current_signature = signature
    if any((n.text or "").strip() for n in current):
        segments.append(current)
    return segments


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


def export_docx_with_translated_text(content: bytes, translated_text: str, page_range: str = "") -> bytes:
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

    w_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    namespaces = {"w": w_ns}
    replacements = {}
    block_index = 0
    for part, document in documents.items():
        root = ElementTree.fromstring(document)
        changed = False
        for paragraph in docx_paragraphs_in_range(
                root, w_ns, namespaces, page_range if part == "word/document.xml" else ""):
            for text_nodes in docx_paragraph_run_segments(paragraph, w_ns):
                if block_index >= len(blocks):
                    break
                # A run that is only spaces or tabs is usually alignment padding before a tab
                # stop (a right-aligned signature line splits into a run of leading spaces, tab
                # runs outside <w:t> entirely, then the name in its own run) - dumping the
                # translation into whichever run happens to sit at index 0 moved translated text
                # ahead of its tab stops and dropped the font size the real content run carried.
                # Target the first run that actually has a word in it instead, and leave
                # whitespace-only runs untouched.
                original = "".join(node.text or "" for node in text_nodes)
                content_indices = [index for index, node in enumerate(text_nodes) if (node.text or "").strip()]
                target_index = content_indices[0] if content_indices else 0
                # A segment boundary triggered by a formatting change (not a tab) has no other
                # separator - "wird dann " and "fett und wichtig" ran together as
                # "wird dannfett und wichtig" once each segment's own translation was stripped for
                # the model. Putting back whatever leading/trailing space the original run had
                # keeps the same visual join the untranslated document had.
                leading = original[:len(original) - len(original.lstrip())]
                trailing = original[len(original.rstrip()):]
                for node_index, node in enumerate(text_nodes):
                    if node_index == target_index:
                        node.text = leading + blocks[block_index] + trailing
                    elif node_index in content_indices:
                        node.text = ""
                block_index += 1
                changed = True
            if block_index >= len(blocks):
                break
        if changed:
            replacements[part] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        if block_index >= len(blocks):
            break
    return write_zip_with_replacement(content, replacements)


ODT_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
# text:p is a body paragraph, text:h a heading - two different tags for what looks like the same
# "paragraph" in the rendered document. Querying only text:p silently dropped every heading
# (measured: a two-heading test document lost both from the extracted text).
ODT_PARAGRAPH_TAGS = {f"{{{ODT_TEXT_NS}}}p", f"{{{ODT_TEXT_NS}}}h"}
# Neither has any text of its own - a plain concatenation walk contributes nothing for them,
# gluing the words on either side together the same way <w:tab/> did in DOCX.
ODT_SPACING_TAGS = {f"{{{ODT_TEXT_NS}}}tab", f"{{{ODT_TEXT_NS}}}line-break"}
ODT_LINK_TAG = f"{{{ODT_TEXT_NS}}}a"
ODT_STYLE_NS = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
# fo: in an ODF file is this compatibility namespace, not real XSL-FO - a manual page break
# (Ctrl+Enter in Writer) has no inline marker the way DOCX's <w:br w:type="page"/> does; it sets
# fo:break-before="page" on a paragraph style instead, referenced by the paragraph's own
# text:style-name rather than carried on the paragraph itself.
ODT_FO_NS = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
ODT_STYLE_TAG = f"{{{ODT_STYLE_NS}}}style"
ODT_STYLE_NAME_ATTR = f"{{{ODT_STYLE_NS}}}name"
ODT_PARAGRAPH_PROPERTIES_TAG = f"{{{ODT_STYLE_NS}}}paragraph-properties"
ODT_BREAK_BEFORE_ATTR = f"{{{ODT_FO_NS}}}break-before"
ODT_PARAGRAPH_STYLE_ATTR = f"{{{ODT_TEXT_NS}}}style-name"


def odt_run_entries(element):
    """Yield (node, attr, signature) for each text-bearing position in a paragraph, in document
    order. attr is "text" or "tail" (which attribute on node to read/overwrite), signature is
    True/False for whether that position sits inside a <text:a> hyperlink, and a tab/line-break
    yields (None, None, "BREAK").

    ODF has no dedicated text node the way DOCX/PPTX's <w:t>/<a:t> do: text sits directly as the
    .text/.tail of whichever element it follows (a plain run, a <text:span>, a <text:a> link).
    Bold/italic are not tracked as a signature dimension here the way DOCX/PPTX's inline b/i
    flags are - ODF spans reference a style by name instead of carrying the flag inline, and
    resolving that against the stylesheet is a bigger job than this covers; only tabs, line
    breaks and hyperlinks split a segment.
    """
    def walk(el, in_link):
        in_link = in_link or el.tag == ODT_LINK_TAG
        if el.text:
            yield el, "text", in_link
        for child in el:
            if child.tag in ODT_SPACING_TAGS:
                yield None, None, "BREAK"
                continue
            yield from walk(child, in_link)
            if child.tail:
                yield child, "tail", in_link

    yield from walk(element, False)


def odt_paragraph_run_segments(element) -> List[List[Tuple[Any, str]]]:
    """Group a paragraph's text positions into segments split at tab/line-break marks and at a
    <text:a> hyperlink's boundary, so each translates - and reinjects - independently. A segment
    is a list of (element, "text"|"tail") pairs naming which attribute to overwrite, not a list
    of child nodes - see odt_run_entries for why."""
    segments: List[List[Tuple[Any, str]]] = []
    current: List[Tuple[Any, str]] = []
    current_signature = None
    for node, attr, signature in odt_run_entries(element):
        has_content = any((getattr(el, a) or "").strip() for el, a in current)
        if signature == "BREAK":
            if has_content:
                segments.append(current)
                current = []
                current_signature = None
            continue
        if has_content and signature != current_signature:
            segments.append(current)
            current = []
        current.append((node, attr))
        current_signature = signature
    if any((getattr(el, a) or "").strip() for el, a in current):
        segments.append(current)
    return segments


def odt_page_break_style_names(root) -> Set[str]:
    """Names of paragraph styles carrying a manual page break, so odt_paragraphs_in_range can
    tell which paragraphs start a new page from their text:style-name alone."""
    names = set()
    for style in root.iter(ODT_STYLE_TAG):
        properties = style.find(ODT_PARAGRAPH_PROPERTIES_TAG)
        if properties is not None and properties.get(ODT_BREAK_BEFORE_ATTR) == "page":
            name = style.get(ODT_STYLE_NAME_ATTR)
            if name:
                names.add(name)
    return names


def odt_paragraph_page_numbers(paragraphs, break_style_names: Set[str]) -> List[int]:
    pages = []
    page = 1
    for paragraph in paragraphs:
        if paragraph.get(ODT_PARAGRAPH_STYLE_ATTR) in break_style_names:
            page += 1
        pages.append(page)
    return pages


def odt_paragraphs_in_range(root, page_range: str) -> List[Any]:
    paragraphs = [element for element in root.iter() if element.tag in ODT_PARAGRAPH_TAGS]
    if not page_range.strip():
        return paragraphs
    page_numbers = odt_paragraph_page_numbers(paragraphs, odt_page_break_style_names(root))
    total_pages = page_numbers[-1] if page_numbers else 1
    selected = set(parse_page_range(page_range, total_pages))
    return [paragraph for paragraph, page in zip(paragraphs, page_numbers) if page in selected]


def extract_odt_text_from_bytes(content: bytes, page_range: str = "") -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as odt:
            ensure_zip_size(odt)
            document = odt.read("content.xml")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read ODT: {exc}") from exc

    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse ODT XML: {exc}") from exc

    paragraphs = []
    for element in odt_paragraphs_in_range(root, page_range):
        for segment in odt_paragraph_run_segments(element):
            text = re.sub(r"\s+", " ", "".join(getattr(el, attr) or "" for el, attr in segment)).strip()
            if text:
                paragraphs.append(text)

    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in ODT")
    return result


def export_odt_with_translated_text(content: bytes, translated_text: str, page_range: str = "") -> bytes:
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

    root = ElementTree.fromstring(document)
    block_index = 0
    for element in odt_paragraphs_in_range(root, page_range):
        for segment in odt_paragraph_run_segments(element):
            if block_index >= len(blocks):
                break
            # Same reasoning as export_docx_with_translated_text: target the first position that
            # actually has a word in it, and restore the segment's own original leading/trailing
            # whitespace, since a link- or break-triggered boundary otherwise has nothing left to
            # keep the words on either side visually apart.
            original = "".join(getattr(el, attr) or "" for el, attr in segment)
            content_positions = [i for i, (el, attr) in enumerate(segment) if (getattr(el, attr) or "").strip()]
            target_index = content_positions[0] if content_positions else 0
            leading = original[:len(original) - len(original.lstrip())]
            trailing = original[len(original.rstrip()):]
            for index, (el, attr) in enumerate(segment):
                if index == target_index:
                    setattr(el, attr, leading + blocks[block_index] + trailing)
                elif index in content_positions:
                    setattr(el, attr, "")
            block_index += 1
        if block_index >= len(blocks):
            break
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

    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    namespaces = {"a": a_ns}
    paragraphs = []
    for part, document in documents.items():
        try:
            root = ElementTree.fromstring(document)
        except ElementTree.ParseError as exc:
            raise HTTPException(status_code=400, detail=f"Could not parse PPTX XML {part}: {exc}") from exc
        for paragraph in root.findall(".//a:p", namespaces):
            for segment in pptx_paragraph_run_segments(paragraph, a_ns):
                text = re.sub(r"\s+", " ", "".join(node.text or "" for node in segment)).strip()
                if text:
                    paragraphs.append(text)

    result = "\n\n".join(paragraphs).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in PPTX")
    return result


def pptx_run_entries(paragraph, a_ns: str):
    """Yield (node, signature) for each <a:t>/<a:br>/<a:tab> in a paragraph, in document order.

    signature is (bold, italic, is_hyperlink) for a text node, or the string "BREAK" for a
    tab/line-break. Recurses into every descendant rather than assuming runs sit directly under
    <a:p> - a run's own <a:rPr b="1"/i="1"> carries its bold/italic, and a hyperlink is an
    <a:hlinkClick> child of that same rPr (not a separate wrapping element the way DOCX does it),
    so the signature only needs picking up once, on entering the run itself.
    """
    r_tag, rpr_tag = f"{{{a_ns}}}r", f"{{{a_ns}}}rPr"
    text_tag = f"{{{a_ns}}}t"
    space_tags = {f"{{{a_ns}}}br", f"{{{a_ns}}}tab"}
    hlink_tag = f"{{{a_ns}}}hlinkClick"

    def walk(element, signature):
        if element.tag == r_tag:
            rpr = element.find(rpr_tag)
            signature = (
                rpr is not None and rpr.get("b") == "1",
                rpr is not None and rpr.get("i") == "1",
                rpr is not None and rpr.find(hlink_tag) is not None,
            )
        if element.tag == text_tag:
            yield element, signature
        elif element.tag in space_tags:
            yield element, "BREAK"
        for child in element:
            yield from walk(child, signature)

    yield from walk(paragraph, (False, False, False))


def pptx_paragraph_run_segments(paragraph, a_ns: str) -> List[List[Any]]:
    """Group a paragraph's text nodes into segments split at tab/line-break marks and wherever
    the run's bold/italic/hyperlink signature changes with real content on both sides.

    A "label<tab>value" line needs its two sides translated independently (see
    docx_paragraph_run_segments for the tab case) - a run of different formatting mid-sentence
    is the same problem without a tab: translating "Weitere Informationen ... <link>unserer
    Webseite</link> ... Internet." as one block put the whole sentence in the first run and
    blanked the hyperlink's run, leaving a hyperlink with no visible, clickable text at all. A
    formatting change on a run that is still empty (leading padding, e.g.) does not start a new
    segment - same "leave whitespace-only content where it is" reasoning as the tab case.
    """
    segments: List[List[Any]] = []
    current: List[Any] = []
    current_signature = None
    for node, signature in pptx_run_entries(paragraph, a_ns):
        has_content = any((n.text or "").strip() for n in current)
        if signature == "BREAK":
            if has_content:
                segments.append(current)
                current = []
                current_signature = None
            continue
        if has_content and signature != current_signature:
            segments.append(current)
            current = []
        current.append(node)
        current_signature = signature
    if any((n.text or "").strip() for n in current):
        segments.append(current)
    return segments


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

    a_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    namespaces = {"a": a_ns}
    replacements = {}
    block_index = 0
    for part, document in documents.items():
        root = ElementTree.fromstring(document)
        changed = False
        for paragraph in root.findall(".//a:p", namespaces):
            for text_nodes in pptx_paragraph_run_segments(paragraph, a_ns):
                if block_index >= len(blocks):
                    break
                # Same reasoning as export_docx_with_translated_text: skip whitespace-only runs
                # when choosing where the translation goes, so alignment padding and the real
                # content run's formatting both survive - and restore whatever leading/trailing
                # space the original had, since a formatting-triggered segment boundary has no
                # tab to keep the words visually apart on its own.
                original = "".join(node.text or "" for node in text_nodes)
                content_indices = [index for index, node in enumerate(text_nodes) if (node.text or "").strip()]
                target_index = content_indices[0] if content_indices else 0
                leading = original[:len(original) - len(original.lstrip())]
                trailing = original[len(original.rstrip()):]
                for node_index, node in enumerate(text_nodes):
                    if node_index == target_index:
                        node.text = leading + blocks[block_index] + trailing
                    elif node_index in content_indices:
                        node.text = ""
                block_index += 1
                changed = True
            if block_index >= len(blocks):
                break
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


def csv_dict_reader(content: bytes) -> csv.DictReader:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode CSV as UTF-8: {exc}") from exc
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=422, detail="CSV has no header row")
    return reader


def extract_csv_text_from_bytes(content: bytes, columns: str) -> str:
    selected_columns = parse_column_names(columns)
    if not selected_columns:
        raise HTTPException(status_code=400, detail="Select at least one CSV column")

    reader = csv_dict_reader(content)
    missing = [column for column in selected_columns if column not in reader.fieldnames]
    if missing:
        raise HTTPException(status_code=400, detail="CSV columns not found: " + ", ".join(missing))

    rows = []
    for row in reader:
        for column in selected_columns:
            value = row.get(column, "").strip()
            if value:
                rows.append(value)

    result = "\n\n".join(rows).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in selected CSV columns")
    return result


def export_csv_with_translated_text(content: bytes, columns: str, translated_text: str) -> bytes:
    selected_columns = parse_column_names(columns)
    if not selected_columns:
        raise HTTPException(status_code=400, detail="Select at least one CSV column")
    reader = csv_dict_reader(content)
    missing = [column for column in selected_columns if column not in reader.fieldnames]
    if missing:
        raise HTTPException(status_code=400, detail="CSV columns not found: " + ", ".join(missing))

    rows = list(reader)
    blocks = translated_blocks(translated_text)
    # One block per non-empty cell, in the same row-major, column order extraction produced -
    # not one block per row split back apart on " | ". Measured: the model dropped that
    # separator entirely ("Blauer Stuhl | Ein bequemer..." came back "Blue chair A comfortable
    # ..."), which silently misaligned every column after the first.
    block_index = 0
    for row in rows:
        for column in selected_columns:
            if row.get(column, "").strip() and block_index < len(blocks):
                row[column] = blocks[block_index]
                block_index += 1

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def extract_csv_auto_text_from_bytes(content: bytes) -> str:
    """No columns given at all: translate every cell that looks like real text, across every
    column, rather than requiring the user to name one - see spreadsheet_value_looks_translatable
    for what gets skipped."""
    reader = csv_dict_reader(content)
    blocks = []
    for row in reader:
        for column in reader.fieldnames:
            value = row.get(column, "").strip()
            if spreadsheet_value_looks_translatable(value):
                blocks.append(value)

    result = "\n\n".join(blocks).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No translatable text found in this CSV")
    return result


def export_csv_auto_with_translated_text(content: bytes, translated_text: str) -> bytes:
    reader = csv_dict_reader(content)
    rows = list(reader)
    blocks = translated_blocks(translated_text)
    block_index = 0
    for row in rows:
        for column in reader.fieldnames:
            value = row.get(column, "").strip()
            if spreadsheet_value_looks_translatable(value) and block_index < len(blocks):
                row[column] = blocks[block_index]
                block_index += 1

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def xlsx_column_name(cell_ref: str) -> str:
    return re.sub(r"[^A-Z]", "", cell_ref.upper())


def xlsx_column_index(name: str) -> int:
    """B < Z < AA < AB - a plain alphabetical sort puts "AA" before "B" instead of after."""
    index = 0
    for char in name:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


# Cells a "translate the whole document" pass should leave alone even though they're non-empty:
# an IP, a MAC address, a plain number or an IP range read as prose come back mangled or renamed
# (an address is not a sentence), and none of them need translating in the first place. Not
# exhaustive - anything not matching one of these is assumed to be real text and gets translated.
SPREADSHEET_SKIP_PATTERNS = [
    re.compile(r"^-?\d+([.,]\d+)?$"),  # a plain number, e.g. 49.99 or 199,99 or a port/VLAN id
    re.compile(r"^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$"),  # IPv4 address or CIDR block
    re.compile(r"^(\d{1,3}\.){3}\d{1,3}\s*-\s*(\d{1,3}\.){3}\d{1,3}$"),  # IPv4 range
    re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$"),  # MAC address
]


def spreadsheet_value_looks_translatable(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    return not any(pattern.match(value) for pattern in SPREADSHEET_SKIP_PATTERNS)


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


def xlsx_all_sheets(workbook: zipfile.ZipFile) -> List[Tuple[str, str]]:
    """Every sheet's (name, part path), in workbook order."""
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

    result = []
    for sheet in sheets:
        rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = rels.get(rid)
        if not target:
            continue
        result.append((sheet.attrib.get("name", ""), xlsx_relationship_target(target)))
    return result


def xlsx_sheet_path(workbook: zipfile.ZipFile, sheet_name: str) -> str:
    sheets = xlsx_all_sheets(workbook)
    if sheet_name.strip():
        for name, path in sheets:
            if name == sheet_name.strip():
                return path
        raise HTTPException(status_code=400, detail=f"XLSX sheet not found: {sheet_name}")
    return sheets[0][1]


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


def xlsx_header_row_index(rows: List[Dict[str, Any]], selected_columns: List[str]) -> int:
    """Find the row that actually names the requested columns, rather than assuming it is
    whatever row happens to be first. A hand-built sheet commonly has a title or spacer row
    above its real header (measured: a real spreadsheet's first populated row was "Router
    Informationen", several rows above the row naming "Hostname"/"Kategorie"/"IP") - treating
    that as the header meant those names were never found in it, fell back to being read as a
    literal column letter, and were then reported "not found" since no such letter existed
    either. Falls back to row 0 if none contain a requested name (e.g. all-letter selectors).
    """
    wanted = {column.strip().lower() for column in selected_columns}
    for index, row in enumerate(rows):
        values_lower = {str(value).strip().lower() for value in row["values"].values() if value}
        if wanted & values_lower:
            return index
    return 0


def resolve_xlsx_columns(rows: List[Dict[str, Any]], header_index: int, selected_columns: List[str]) -> List[str]:
    if not rows:
        raise HTTPException(status_code=422, detail="XLSX sheet has no rows")
    header = {value: column for column, value in rows[header_index]["values"].items() if value}
    resolved_columns = [header.get(column, column.upper()) for column in selected_columns]
    data_rows = rows[header_index + 1:]
    missing = [column for column in resolved_columns if all(not row["values"].get(column) for row in data_rows)]
    if missing:
        raise HTTPException(status_code=400, detail="XLSX columns not found: " + ", ".join(missing))
    return resolved_columns


def extract_xlsx_text_from_bytes(content: bytes, sheet_name: str, columns: str) -> str:
    selected_columns = parse_column_names(columns)
    if not selected_columns:
        raise HTTPException(status_code=400, detail="Select at least one XLSX column")

    try:
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            ensure_zip_size(workbook)
            shared = xlsx_shared_strings(workbook)
            sheet_path = xlsx_sheet_path(workbook, sheet_name)
            root = ElementTree.fromstring(workbook.read(sheet_path))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read XLSX: {exc}") from exc

    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = xlsx_rows_with_values(root, shared, namespace)
    header_index = xlsx_header_row_index(rows, selected_columns)
    resolved_columns = resolve_xlsx_columns(rows, header_index, selected_columns)

    output_rows = []
    for row in rows[header_index + 1:]:
        for column in resolved_columns:
            value = row["values"].get(column, "").strip()
            if value:
                output_rows.append(value)

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
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            ensure_zip_size(workbook)
            shared = xlsx_shared_strings(workbook)
            sheet_path = xlsx_sheet_path(workbook, sheet_name)
            sheet_xml = workbook.read(sheet_path)
            root = ElementTree.fromstring(sheet_xml)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read XLSX: {exc}") from exc

    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = xlsx_rows_with_values(root, shared, namespace)
    header_index = xlsx_header_row_index(rows, selected_columns)
    resolved_columns = resolve_xlsx_columns(rows, header_index, selected_columns)
    blocks = translated_blocks(translated_text)
    # Same reasoning as export_csv_with_translated_text: one block per non-empty cell, not one
    # block per row split back apart on a separator the model doesn't reliably keep.
    block_index = 0
    for row_info in rows[header_index + 1:]:
        for column in resolved_columns:
            if not row_info["values"].get(column, "").strip():
                continue
            if block_index >= len(blocks):
                break
            cell = row_info["cells"].get(column)
            if cell is not None:
                xlsx_set_cell_text(cell, blocks[block_index])
            block_index += 1

    return write_zip_with_replacement(content, {sheet_path: ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)})


def xlsx_is_multi_sheet_spec(columns: str) -> bool:
    """A "Sheet: col1,col2" line looks nothing like a plain column list ("title,description" or
    "A,B") - the colon is the tell, checked per line since a single-sheet column name could
    itself contain no colon at all."""
    return any(":" in line for line in columns.splitlines() if line.strip())


def parse_xlsx_sheet_columns(spec: str) -> List[Tuple[str, str]]:
    pairs = []
    for line in spec.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise HTTPException(status_code=400, detail=f'Each line must be "Sheet: columns" - got: {line}')
        sheet_name, columns = line.split(":", 1)
        sheet_name, columns = sheet_name.strip(), columns.strip()
        if not sheet_name or not columns:
            raise HTTPException(status_code=400, detail=f'Each line must be "Sheet: columns" - got: {line}')
        pairs.append((sheet_name, columns))
    if not pairs:
        raise HTTPException(status_code=400, detail="No sheet/column lines given")
    return pairs


def extract_xlsx_multi_sheet_text_from_bytes(content: bytes, spec: str) -> str:
    """Extract several sheets in one pass, each with its own column selection - a hand-built
    workbook's sheets rarely share a header row (measured: "Hostname,Kategorie,IP" on one sheet,
    "Name,Beschreibung" on another), so a single shared column list can't cover more than one.
    A sheet with nothing to translate for its columns is skipped rather than failing the whole
    request - some sheets in a real multi-sheet selection legitimately have no matching rows.
    """
    parts = []
    for sheet_name, columns in parse_xlsx_sheet_columns(spec):
        try:
            parts.append(extract_xlsx_text_from_bytes(content, sheet_name, columns))
        except HTTPException as exc:
            if exc.status_code == 422:
                continue
            raise
    result = "\n\n".join(parts).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No text found in any selected sheet/column")
    return result


def export_xlsx_multi_sheet_with_translated_text(content: bytes, spec: str, translated_text: str) -> bytes:
    """Write translated blocks back across several sheets, in the same order
    extract_xlsx_multi_sheet_text_from_bytes produced them in.

    Each sheet's own block count is recomputed from the original content rather than persisted
    anywhere - re-running the same extraction is deterministic and exactly mirrors what was
    counted the first time, so there's nothing to keep in sync. export_xlsx_with_translated_text
    is then chained sheet by sheet: each call's output (all sheets untouched except the one just
    written) becomes the next call's input, since every sheet lives in its own XML part inside
    the archive and never touches another sheet's part.
    """
    blocks = translated_blocks(translated_text)
    block_index = 0
    current_content = content
    for sheet_name, columns in parse_xlsx_sheet_columns(spec):
        try:
            sheet_text = extract_xlsx_text_from_bytes(content, sheet_name, columns)
        except HTTPException as exc:
            if exc.status_code == 422:
                continue
            raise
        count = len(translated_blocks(sheet_text))
        sheet_blocks = blocks[block_index:block_index + count]
        block_index += count
        if sheet_blocks:
            current_content = export_xlsx_with_translated_text(
                current_content, sheet_name, columns, "\n\n".join(sheet_blocks)
            )
    return current_content


def extract_xlsx_auto_text_from_bytes(content: bytes) -> str:
    """No sheet or columns given at all: translate every cell across every sheet that looks like
    real text - see spreadsheet_value_looks_translatable for what gets skipped. Unlike the named-
    column paths, this doesn't try to single out a header row first (there's no requested column
    name to search for) - a header label like "Hostname" is itself just more text to translate.
    """
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    blocks = []
    try:
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            ensure_zip_size(workbook)
            shared = xlsx_shared_strings(workbook)
            for _sheet_name, sheet_path in xlsx_all_sheets(workbook):
                root = ElementTree.fromstring(workbook.read(sheet_path))
                for row in xlsx_rows_with_values(root, shared, namespace):
                    for column in sorted(row["values"], key=xlsx_column_index):
                        if spreadsheet_value_looks_translatable(row["values"][column]):
                            blocks.append(row["values"][column].strip())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read XLSX: {exc}") from exc

    result = "\n\n".join(blocks).strip()
    if not result:
        raise HTTPException(status_code=422, detail="No translatable text found in this workbook")
    return result


def export_xlsx_auto_with_translated_text(content: bytes, translated_text: str) -> bytes:
    blocks = translated_blocks(translated_text)
    namespace = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    replacements = {}
    block_index = 0
    try:
        with zipfile.ZipFile(BytesIO(content)) as workbook:
            ensure_zip_size(workbook)
            shared = xlsx_shared_strings(workbook)
            for _sheet_name, sheet_path in xlsx_all_sheets(workbook):
                root = ElementTree.fromstring(workbook.read(sheet_path))
                changed = False
                for row in xlsx_rows_with_values(root, shared, namespace):
                    for column in sorted(row["values"], key=xlsx_column_index):
                        if not spreadsheet_value_looks_translatable(row["values"][column]):
                            continue
                        if block_index >= len(blocks):
                            continue
                        cell = row["cells"].get(column)
                        if cell is not None:
                            xlsx_set_cell_text(cell, blocks[block_index])
                        block_index += 1
                        changed = True
                if changed:
                    replacements[sheet_path] = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read XLSX: {exc}") from exc
    return write_zip_with_replacement(content, replacements)


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
        if "-->" not in part:
            continue
        if block_index >= len(blocks):
            break
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


async def extract_pdf_markdown(file: UploadFile, page_range: str = "", source: str = AUTO_SOURCE) -> str:
    content = await read_upload_bytes(file, "PDF")
    return extract_pdf_markdown_from_bytes(content, file.content_type or "application/pdf", page_range, source)


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
        if source == AUTO_SOURCE:
            source = detect_source_language(text)
            update_job(job_id, source=source)
        update_job(job_id, status="running", message="Model loading or translation running", started_at=time.time())
        if source_extension == "md":
            # Headings/lists/quotes need their markers kept out of what the model sees - see
            # translate_markdown_document.
            result, chunk_count = translate_markdown_document(text, source, target, job_id)
        else:
            # One paragraph per translate_batch entry, not the whole text in one translate_one
            # call: translate_one/translate_batch sentence-split internally and rejoin with a
            # single space, so a multi-paragraph text translated as one blob comes back with every
            # blank line gone. Harmless for the Text Field's own output, but every original-format
            # export (export_docx_with_translated_text and its siblings) maps translated_blocks()
            # back onto the source by splitting on blank lines - measured on a 6-paragraph DOCX:
            # the whole translation landed in paragraph 1, the other five kept their German text
            # verbatim, since translated_blocks() saw only one block once the breaks were gone.
            paragraphs = [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()] or [text]
            update_job(job_id, total=len(paragraphs), message=f"Translating 0 / {len(paragraphs)} paragraphs")
            result = "\n\n".join(translate_chunks_batched(paragraphs, source, target, job_id, PDF_LAYOUT_BATCH_SIZE))
            chunk_count = len(paragraphs)
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
            current=chunk_count,
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
    parts = re.split(r"(?m)^# Page ", markdown)
    if len(parts) <= 1:
        # No "# Page N" marker anywhere, e.g. any non-PDF-sourced translation: re.split still
        # returns the whole text as one part, which used to get its first line torn off and
        # misread as a page number/heading instead of being treated as plain body text.
        return []
    sections = []
    for section in parts:
        section = section.strip()
        if not section:
            continue
        page_number, _, page_text = section.partition("\n")
        sections.append((page_number.strip(), page_text.strip()))
    return sections


def merge_cross_page_sentences(sections: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Move a page's trailing incomplete sentence onto the next page's text.

    Pages are joined with a hard break regardless of where a sentence actually ends, so a
    sentence that runs across a page boundary in the source PDF is cut into two fragments -
    each translated on its own, neither a complete sentence. Measured on a real OCR page: the
    tail end of one such fragment came back as an invented sentence, the other as a plausible
    translation rejected by guard_hallucination for running slightly long, because neither half
    read as the sentence it actually was. Carrying the trailing fragment forward before chunking
    keeps the sentence whole for translation; the page marker it ends up under shifts by one
    sentence, no worse than the original PDF already showing the same split across the same two
    pages.
    """
    sections = list(sections)
    for index in range(len(sections) - 1):
        number, text = sections[index]
        if not text:
            continue
        matches = list(SENTENCE_END.finditer(text))
        cut = matches[-1].end() if matches else 0
        if cut == 0 or cut >= len(text):
            continue
        carry = text[cut:].strip()
        if not carry:
            continue
        next_number, next_text = sections[index + 1]
        sections[index] = (number, text[:cut].strip())
        sections[index + 1] = (next_number, (carry + " " + next_text).strip())
    return sections


def run_pdf_translate_job(
    job_id: str,
    content: bytes,
    content_type: str,
    source: str,
    target: str,
    filename: str,
    page_range: str = "",
    layout_fallback: bool = False,
):
    try:
        # A scanned page with no source language set is read twice, which takes noticeably longer,
        # so the message says why instead of letting the job look stuck.
        extracting = "Extracting PDF (detecting scan language)" if source == AUTO_SOURCE else "Extracting PDF"
        update_job(job_id, status="running", message=extracting, started_at=time.time())
        markdown = extract_pdf_markdown_from_bytes(content, content_type, page_range, source)
        if source == AUTO_SOURCE:
            source = detect_source_language(markdown)
            update_job(job_id, source=source)
        markdown = expand_lowercase_ligatures(markdown, source)
        sections = merge_cross_page_sentences(pdf_sections(markdown))
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
        complete_message = (
            "Complete (no positioned text on the selected pages, used plain export instead of layout)"
            if layout_fallback else "Complete"
        )
        update_job(
            job_id,
            status="complete",
            message=complete_message,
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
    page_range: str = "",
):
    try:
        update_job(job_id, status="running", message="Reading PDF layout", started_at=time.time())
        try:
            pages = extract_pdf_layout(content, page_range)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            # Scanned/image-only PDF: no positioned text to lay a translation back over, so this
            # falls back to the plain extract-then-translate pipeline, which has an OCR fallback.
            run_pdf_translate_job(job_id, content, "application/pdf", source, target, filename, page_range, layout_fallback=True)
            return
        wait_if_paused_or_cancelled(job_id)
        # Widget field values are interleaved after their page's ordinary paragraphs, not
        # collected separately: the stored translated_text is one flat "\n\n"-joined sequence
        # matched to extract_pdf_layout's output purely by position (history re-export re-runs
        # that same extraction and zips the blocks back on), so a field's translation has to sit
        # at the exact position render_pdf_layout_overlay will look for it at, see there.
        paragraphs = [item["text"] for page in pages
                      for item in page["paragraphs"] + page["widgets"]]
        if source == AUTO_SOURCE:
            source = detect_source_language("\n\n".join(paragraphs))
            update_job(job_id, source=source)
        # Now that the language is settled, see expand_lowercase_ligatures. Only the text handed
        # to the model is repaired; placement runs off the lines' coordinates, and a paragraph
        # count that stayed the same keeps the re-export lined up.
        paragraphs = [expand_lowercase_ligatures(text, source) for text in paragraphs]
        # One paragraph per chunk: the overlay maps translations back to paragraphs by position,
        # and the model does not reliably keep paragraph breaks inside a single chunk. Several
        # chunks are still translated per model call (translate_chunks_batched), via the tensor's
        # batch dimension rather than concatenated text, so this mapping stays exact.
        chunks: List[str] = []
        chunk_counts: List[int] = []
        for paragraph in paragraphs:
            # Fragments with no word in them keep their original: see has_translatable_text.
            parts = split_long_text(paragraph, MAX_CHARS) if has_translatable_text(paragraph) else []
            chunks.extend(parts)
            chunk_counts.append(len(parts))
        update_job(job_id, total=len(chunks), message=f"Translating 0 / {len(chunks)} chunks")
        translated_chunks = translate_chunks_batched(chunks, source, target, job_id, PDF_LAYOUT_BATCH_SIZE)

        translated: List[str] = []
        position = 0
        for paragraph, count in zip(paragraphs, chunk_counts):
            if not count:
                # Untranslated fragment: keep the original so the paragraph still has an entry and
                # every translation after it stays on its own paragraph.
                translated.append(re.sub(r"\s+", " ", paragraph).strip())
                continue
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
            {"layout": "true", "page_range": page_range},
        )
        update_job(job_id, message="Rendering PDF")
        try:
            history_original_export(history_id, "pdf", content, result,
                                    {"layout": "true", "page_range": page_range})
        except Exception:
            # The translation is done and saved; only the head start on the download is lost, and
            # the download builds it again. Failing the whole job over that would throw away work.
            pass
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
        "version": app.version,
        "fallback_model": FALLBACK_MODEL_ID,
        "dedicated_pairs": len(OPUS_PAIRS),
        "device": selected_device(),
        # Not a setting any more, but the UI's character counter works from it.
        "max_chars": MAX_CHARS,
        "max_file_mb": MAX_FILE_MB,
        "ocr_available": ocr_available,
        "ocr_languages": list(installed_ocr_languages()) if ocr_available else [],
        "model_idle_seconds": MODEL_IDLE_SECONDS,
        "cpu_threads": CPU_THREADS,
        "model_loaded": model_cache_loaded(),
        "root_path": ROOT_PATH,
        "timezone": HISTORY_TIMEZONE,
        "time_format": TIME_FORMAT,
    }


@app.get("/languages")
def languages():
    dedicated_pairs = []
    for key in OPUS_PAIRS:
        source, _, target = key.partition(">")
        if source in CORE_LANGUAGES and target in CORE_LANGUAGES:
            dedicated_pairs.append([CORE_LANGUAGES[source], CORE_LANGUAGES[target]])
    return {
        # The picker starts on auto-detect; DEFAULT_SOURCE is only where failed detection lands.
        "source_default": AUTO_SOURCE,
        "target_default": DEFAULT_TARGET,
        "ui_language": UI_LANGUAGE,
        "languages": [{"code": code, "name": code} for code in language_codes()],
        "favorites": FAVORITE_LANGUAGES,
        "dedicated_pairs": dedicated_pairs,
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
    ensure_known_language(request.source, allow_auto=True)
    ensure_known_language(request.target)
    ensure_queue_workers()
    text = "\n\n".join(str(item) for item in request.q) if isinstance(request.q, list) else str(request.q)
    job_id = create_job("translate", request.source, request.target, "Text.txt")
    update_job(job_id, text=text, original_name="text.txt", source_extension="txt")
    register_job_runner(job_id, run_text_job, (job_id, text, request.source, request.target, "text.txt", b"", "txt"))
    return {"job_id": job_id}


@app.post("/jobs/translate-file")
async def start_translate_file_job(
    file: UploadFile = File(...),
    text: str = Form(""),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
    columns: str = Form(""),
    sheet_name: str = Form(""),
    page_range: str = Form(""),
):
    ensure_known_language(source, allow_auto=True)
    ensure_known_language(target)
    ensure_queue_workers()
    content = await read_upload_bytes(file, "Source file")
    filename = file.filename or "source"
    extension = file_extension(filename)
    job_id = create_job("translate", source, target, filename)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    source_payload_path = job_payload_path(job_id).with_suffix(".source")
    source_payload_path.write_bytes(content)
    source_meta = {"columns": columns, "sheet_name": sheet_name, "page_range": page_range}
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
    ensure_known_language(source, allow_auto=True)
    ensure_known_language(target)
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
    )
    register_job_runner(
        job_id,
        run_pdf_translate_job,
        (job_id, content, file.content_type or "application/pdf", source, target, filename, page_range),
    )
    return {"job_id": job_id}


@app.post("/jobs/translate-pdf-layout")
async def start_translate_pdf_layout_job(
    file: UploadFile = File(...),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
    page_range: str = Form(""),
):
    ensure_known_language(source, allow_auto=True)
    ensure_known_language(target)
    ensure_queue_workers()
    content = await read_upload_bytes(file, "PDF")
    filename = file.filename or "pdf"
    job_id = create_job("translate-pdf-layout", source, target, filename)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    payload_path = job_payload_path(job_id)
    payload_path.write_bytes(content)
    update_job(job_id, payload_path=str(payload_path), filename=filename, page_range=page_range)
    register_job_runner(
        job_id,
        run_pdf_layout_translate_job,
        (job_id, content, source, target, filename, page_range),
    )
    return {"job_id": job_id}


@app.get("/history")
def list_history():
    return {"retention_hours": HISTORY_HOURS, "items": history_items()}


@app.get("/history/{item_id}")
def download_history(item_id: str):
    path, _ = history_paths(item_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="History item not found")
    filename = history_download_name(history_item(item_id, required=False), "md")
    return FileResponse(path, media_type="text/markdown", filename=filename)


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
        return export_docx_with_translated_text(content, text, source_meta.get("page_range", ""))
    if extension == "odt":
        return export_odt_with_translated_text(content, text, source_meta.get("page_range", ""))
    if extension == "pptx":
        return export_pptx_with_translated_text(content, text)
    if extension == "csv":
        columns = source_meta.get("columns", "")
        if not columns.strip():
            return export_csv_auto_with_translated_text(content, text)
        return export_csv_with_translated_text(content, columns, text)
    if extension == "xlsx":
        columns = source_meta.get("columns", "")
        if not columns.strip():
            return export_xlsx_auto_with_translated_text(content, text)
        if xlsx_is_multi_sheet_spec(columns):
            return export_xlsx_multi_sheet_with_translated_text(content, columns, text)
        return export_xlsx_with_translated_text(content, source_meta.get("sheet_name", ""), columns, text)
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
            return export_pdf_layout_with_translated_text(content, text, source_meta.get("page_range", ""))
        return create_text_pdf(text, content)
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
    item = history_item(item_id, required=False)
    filename_base = history_download_name(item, "").rstrip(".")
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
            create_text_pdf(text),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'},
        )
    if safe_format == "doc":
        return Response(
            create_text_docx(text),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.docx"'},
        )
    source_extension = file_extension("x." + item.get("source_extension", ""))
    requested_extension = source_extension if safe_format == "original" else file_extension("x." + safe_format)
    source_path = history_source_path(item_id, source_extension) if source_extension else None
    if not source_path or not source_path.exists():
        raise HTTPException(status_code=404, detail="History source file not found")
    if requested_extension != source_extension:
        raise HTTPException(status_code=400, detail="History source can only be exported in its original format")
    source_meta = item.get("source_meta", {})
    content = history_original_export(
        item_id,
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


@app.post("/export-pdf")
def export_pdf(request: PdfExportRequest):
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="No text to export")
    return Response(
        create_text_pdf(request.text),
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
        create_text_docx(request.text),
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
    exported = (
        export_csv_auto_with_translated_text(content, text)
        if not columns.strip()
        else export_csv_with_translated_text(content, columns, text)
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
        exported = export_xlsx_auto_with_translated_text(content, text)
    elif xlsx_is_multi_sheet_spec(columns):
        exported = export_xlsx_multi_sheet_with_translated_text(content, columns, text)
    else:
        exported = export_xlsx_with_translated_text(content, sheet_name, columns, text)
    return Response(
        exported,
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
    export_path = None
    if json_path.exists():
        try:
            item = json.loads(json_path.read_text(encoding="utf-8"))
            source_extension = file_extension("x." + item.get("source_extension", ""))
            if source_extension:
                source_path = history_source_path(item_id, source_extension)
                export_path = history_export_path(item_id, source_extension)
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
    items = history_items()
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
    return FileResponse(APP_DIR / "templates" / "index.html", media_type="text/html")


@app.post("/translate")
def translate(request: TranslateRequest):
    if isinstance(request.q, list):
        translated = [translate_text(str(item), request.source, request.target) for item in request.q]
    else:
        translated = translate_text(str(request.q), request.source, request.target)
    return {"translatedText": translated}


@app.post("/extract-pdf", response_class=PlainTextResponse)
async def extract_pdf(
    file: UploadFile = File(...),
    page_range: str = Form(""),
    source: str = Form(AUTO_SOURCE),
):
    return await extract_pdf_markdown(file, page_range, source)


@app.post("/extract-docx", response_class=PlainTextResponse)
async def extract_docx(file: UploadFile = File(...), page_range: str = Form("")):
    content = await read_upload_bytes(file, "DOCX")
    return extract_docx_text_from_bytes(content, page_range)


@app.post("/extract-odt", response_class=PlainTextResponse)
async def extract_odt(file: UploadFile = File(...), page_range: str = Form("")):
    content = await read_upload_bytes(file, "ODT")
    return extract_odt_text_from_bytes(content, page_range)


@app.post("/extract-pptx", response_class=PlainTextResponse)
async def extract_pptx(file: UploadFile = File(...)):
    content = await read_upload_bytes(file, "PPTX")
    return extract_pptx_text_from_bytes(content)


@app.post("/extract-csv", response_class=PlainTextResponse)
async def extract_csv(file: UploadFile = File(...), columns: str = Form("")):
    content = await read_upload_bytes(file, "CSV")
    if not columns.strip():
        return extract_csv_auto_text_from_bytes(content)
    return extract_csv_text_from_bytes(content, columns)


@app.post("/extract-xlsx", response_class=PlainTextResponse)
async def extract_xlsx(
    file: UploadFile = File(...),
    sheet_name: str = Form(""),
    columns: str = Form(""),
):
    content = await read_upload_bytes(file, "XLSX")
    if not columns.strip():
        return extract_xlsx_auto_text_from_bytes(content)
    if xlsx_is_multi_sheet_spec(columns):
        return extract_xlsx_multi_sheet_text_from_bytes(content, columns)
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
    markdown = await extract_pdf_markdown(file, page_range, source)
    sections = merge_cross_page_sentences(pdf_sections(markdown))
    translated = [f"# Page {page_number}\n\n{translate_text(page_text, source, target)}"
                  for page_number, page_text in sections]
    return "\n\n".join(translated)

