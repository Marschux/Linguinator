# ... existing imports and setup code ...
import base64
import gc
import ipaddress
import math
import os
import re
import secrets
import socket
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
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
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
HISTORY_DAYS = int(env_value("LINGUINATOR_HISTORY_DAYS", "7"))
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
# How far the overlay may shrink the font to make a longer translation fit its original lines.
PDF_LAYOUT_MIN_SCALE = 0.7
# Kept clear of the page edge when a paragraph has nothing to its right.
PDF_LAYOUT_EDGE_MARGIN = 20.0
# How far the first line of a paragraph may be indented past the ones under it and still count
# as part of it. Half an inch is the usual tab; a centred heading sits much further in than that
# and has to stay a paragraph of its own.
PDF_LAYOUT_MAX_INDENT = 40.0
# How far a word gap may be stretched to justify a line, as a multiple of the font's own space.
# Measured: read back, a line set word by word survives up to three times its normal gap and comes
# apart at about 3.9, where MuPDF's extractor returns every word on a line of its own. Wider gaps
# also tear holes into the setting, so a line that would need more stays ragged.
PDF_JUSTIFY_MAX_SPACE = 3.0
# A line opening with one of these is a list item of its own, however it is placed.
PDF_LIST_MARKER = re.compile(r"^\s*(?:[-•‣▪●◦*]|\(?\d{1,3}[.)])\s")
# How far a paragraph may be tightened purely to keep the original's line count. 0.9 because no
# paragraph in the test documents needed more than that to absorb the substitute font's extra
# width; past it, an extra line is the lesser evil.
PDF_LAYOUT_TIGHTEN_SCALE = 0.9
# The same, for a paragraph with no room below it for even one more line - a table cell being the
# usual one. There the extra line does not land in a gap, it lands outside the box, so fitting the
# original line count is worth more type size than elsewhere.
PDF_LAYOUT_BOXED_MIN_SCALE = 0.6
# A reflow taller than this multiple of the paragraph it replaces is not laid out at all; the
# original stays instead. Overflowing by a line or two is normal (German runs longer, and the
# substitute font wider), a block three times the height is the model having invented text, and
# it lands on top of everything below it.
PDF_LAYOUT_MAX_LINE_GROWTH = 3
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
# The four font resources a generated PDF declares, as (bold, serif).
PDF_FONT_FACES = {"F1": (False, False), "F2": (True, False), "F3": (False, True), "F4": (True, True)}
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
    "cs": "ces_Latn", "sv": "swe_Latn", "da": "dan_Latn", "fi": "fin_Latn", "el": "ell_Grek",
    "hu": "hun_Latn", "bg": "bul_Cyrl",
    "ar": "arb_Arab", "zh": "zho_Hans", "ja": "jpn_Jpan", "he": "heb_Hebr",
    "hi": "hin_Deva", "vi": "vie_Latn", "id": "ind_Latn", "tr": "tur_Latn", "sq": "sqi_Latn",
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

APP_DIR = Path(__file__).resolve().parent


def normalized_root_path(value: str) -> str:
    path = value.strip().strip("/")
    return f"/{path}" if path else ""


ROOT_PATH = normalized_root_path(os.getenv("LINGUINATOR_ROOT_PATH", ""))

app = FastAPI(title="Linguinator", version="0.10.0", root_path=ROOT_PATH)
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
# codes name a specific variety: it knows >>ara<< (Arabic) but not >>arb<< (Modern Standard
# Arabic). An unknown prefix is not rejected - the tokenizer just splits it into ordinary subword
# pieces, so the model receives no target signal at all and answers in whatever language it likes.
# Before this mapping, nl>ar came back in Korean. Any language added here must be checked against
# the fallback tokenizer's vocabulary, not assumed.
FALLBACK_LANGUAGE_ALIASES = {"arb": "ara"}


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


SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*\s+")


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
    # brackets stay with their sentence instead of being eaten as part of the separator.
    sentences: List[str] = []
    start = 0
    for match in SENTENCE_END.finditer(text):
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

# A result this much longer than its source is not a translation. The fallback model answers short,
# low-content fragments - a page number, a list marker, a heading - by dumping training data
# ("Der Präsident. — Das Wort hat die Fraktion...", "== Weblinks =="), which the layout pipeline
# then lays out as if it were text and runs across the whole page. Measured against 153 real
# paragraph pairs from the test document: at these values the eleven invented ones are rejected and
# no genuine translation is, the longest of which grew from "My Story" to "Meine Geschichte" (x2.0,
# inside the margin). Applied per sentence, so the factor never has to cover a whole paragraph.
HALLUCINATION_LENGTH_FACTOR = 2.0
HALLUCINATION_LENGTH_MARGIN = 15

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


def guard_hallucination(source: str, translated: str) -> str:
    """Keep the original wherever the model clearly invented rather than translated."""
    if len(translated) > HALLUCINATION_LENGTH_FACTOR * len(source) + HALLUCINATION_LENGTH_MARGIN:
        return source
    if HALLUCINATION_MARKUP.search(translated) and not HALLUCINATION_MARKUP.search(source):
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
            results.append(guard_hallucination(part, decoded))
        joined = normalize_translated_text(" ".join(part.strip() for part in results if part.strip()))
        return guard_hallucination(text, joined)
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
        text = guard_hallucination(source_text, text)
        if text.strip():
            parts.setdefault(owner, []).append(text.strip())
    results = ["" for _ in texts]
    for owner, pieces in parts.items():
        results[owner] = guard_hallucination(texts[owner], normalize_translated_text(" ".join(pieces)))
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


@lru_cache(maxsize=8)
def font_file(bold: bool = False, script: str = "", serif: bool = False) -> str:
    """The font file a face is drawn and measured with, or "" for the base-14 stand-in.

    A base-14 font can only show WinAnsi characters, so any non-Latin target language (Cyrillic,
    Greek, ...) would come out as garbage. `script` (from detect_pdf_script) picks a file that
    actually covers CJK/Arabic/Devanagari/Hebrew instead, where DejaVu Sans has no glyphs at all;
    those fonts are used as-is for "bold" too since covering the script matters more than the
    weight.
    """
    if script and script in PDF_SCRIPT_FONT_CANDIDATES:
        candidates = PDF_SCRIPT_FONT_CANDIDATES[script]
    elif serif:
        candidates = PDF_SERIF_BOLD_CANDIDATES if bold else PDF_SERIF_CANDIDATES
    else:
        candidates = PDF_FONT_BOLD_CANDIDATES if bold else PDF_FONT_CANDIDATES
    for path in candidates:
        if path and Path(path).exists():
            return path
    return ""


@lru_cache(maxsize=8)
def script_font(bold: bool = False, script: str = "", serif: bool = False):
    """The whole font as MuPDF sees it, for measuring. Cached because MuPDF parses the file again
    for every font object, and a document is measured line by line."""
    path = font_file(bold, script, serif)
    if path:
        try:
            return pymupdf.Font(fontfile=path)
        except Exception:
            pass
    return pymupdf.Font(PDF_BASE_FONTS[(bold, serif)])


def subset_font(bold: bool, script: str, serif: bool, codepoints: Set[int]):
    """The same font cut down to the characters the document actually draws.

    MuPDF embeds whatever font it is handed whole, and its own `subset_fonts` cannot cut the
    CFF-based Noto CJK collection back down again ("format error: Index bounds"), which left a
    single page of Japanese weighing 13.7 MB. Subsetting before drawing works for every font
    format and brought that page to 6 KB.
    """
    path = font_file(bold, script, serif)
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
    return script_font(bold, script, serif)


def pdf_measure_text(text: str, size: float, bold: bool = False, serif: bool = False) -> float:
    return script_font(bold, detect_pdf_script(text), serif).text_length(text, fontsize=size)


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


def pdf_font_key(text: str, face: str) -> Tuple[bool, str, bool]:
    """Which font one line is drawn with: (bold, script, serif)."""
    bold, serif = PDF_FONT_FACES.get(face, (False, False))
    return bold, detect_pdf_script(text), serif


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
    document = pymupdf.open()
    # (page index, text, x, y, face, size, color, page height, edge to justify to). The index
    # rather than the page:
    # adding a page invalidates the page objects handed out before it.
    lines: List[Tuple[Any, ...]] = []
    for output_page_number, page_data in enumerate(pages, start=1):
        width = page_data.get("width", PDF_PAGE_WIDTH)
        height = page_data.get("height", PDF_PAGE_HEIGHT)
        margin = page_data.get("margin", PDF_MARGIN)
        document.new_page(width=width, height=height)
        index = output_page_number - 1
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

    codepoints: Dict[Tuple[bool, str, bool], Set[int]] = {}
    for _, text, _, _, face, _, _, _, _ in lines:
        codepoints.setdefault(pdf_font_key(text, face), set()).update(map(ord, text))
    fonts = {key: subset_font(*key, points) for key, points in codepoints.items()}
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


def create_text_pdf(text: str) -> bytes:
    return create_pdf_from_pages(pdf_document_pages(text))


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
    if job.get("kind") == "translate-url":
        # Nothing to restore from disk: the address is the whole input, and it is in the record.
        return run_url_translate_job, (
            job_id,
            job.get("url", ""),
            job.get("source", DEFAULT_SOURCE),
            job.get("target", DEFAULT_TARGET),
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


# The web scraper reaches out to the open internet, which is the one place this otherwise local
# tool talks to a stranger. Constants rather than env vars on purpose: nothing here is worth
# tuning per install, and the address rules are a safety property, not a preference.
SCRAPER_TIMEOUT_SECONDS = 15.0
SCRAPER_MAX_REDIRECTS = 5
# Identifies the tool and points at the project, which is what sites like Wikipedia ask of any
# non-browser client; a bare product name gets a 403 there.
SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (compatible; Linguinator/1.0; +https://gitlab.com/uncoded-bytes/Linguinator) "
    "user-initiated single-page fetch"
)
# Shorter than this is not an article: a page that redirects with JavaScript leaves a stub that
# extracts to a couple of dozen characters, and shipping that as a "translation" is worse than
# saying it did not work.
SCRAPER_MIN_CHARS = 200
SCRAPER_HTML_TYPES = ("text/html", "application/xhtml+xml")
# Link-local covers 169.254.0.0/16 and with it the cloud metadata address 169.254.169.254.
SCRAPER_BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost")


def ensure_public_url(url: str) -> str:
    """Reject anything that is not a public http(s) address, and say why.

    Whoever can reach the UI can make the container fetch an address of their choosing. Without
    this, that means everything the container can reach: the router's web interface, a sibling
    container on proxy-net, the app's own /health - and the answer comes back as a neat PDF.
    Every address the hostname resolves to is checked, not just the first, since a name can
    return both a public and a private one.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http:// and https:// addresses can be fetched")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="That address has no host name")
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(SCRAPER_BLOCKED_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail=f"{host} is a local address, only public pages can be fetched")
    try:
        resolved = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"Could not look up {host}: {exc}") from exc
    for entry in resolved:
        address = ipaddress.ip_address(entry[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            raise HTTPException(
                status_code=400,
                detail=f"{host} resolves to {address}, an address inside your own network. "
                       f"Only pages on the public internet can be fetched.",
            )
    return url.strip()


def fetch_web_page(url: str) -> Tuple[bytes, str]:
    """Fetch a page as HTML, following redirects one at a time, and return it with its final URL.

    Redirects are followed by hand rather than by httpx so every hop goes through
    ensure_public_url: a redirect to an internal address is the ordinary way past a check that
    only looks at the address the user typed.

    Known limit: this does not defend against DNS rebinding, where a name resolves to a public
    address for the check and a private one for the connection a moment later. Closing that
    would mean connecting to the vetted IP and setting the Host header by hand, which is more
    machinery than a tool on a home network warrants.
    """
    current = ensure_public_url(url)
    with httpx.Client(follow_redirects=False, timeout=SCRAPER_TIMEOUT_SECONDS) as client:
        for _hop in range(SCRAPER_MAX_REDIRECTS + 1):
            try:
                with client.stream("GET", current, headers={"User-Agent": SCRAPER_USER_AGENT}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise HTTPException(status_code=502, detail="The page redirected without saying where")
                        current = ensure_public_url(urljoin(current, location))
                        continue
                    if response.status_code >= 400:
                        raise HTTPException(
                            status_code=502,
                            detail=f"The page answered with HTTP {response.status_code}",
                        )
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type and content_type not in SCRAPER_HTML_TYPES:
                        raise HTTPException(
                            status_code=415,
                            detail=f"That address serves {content_type}, not a web page. "
                                   f"Files can be translated through the other tabs.",
                        )
                    chunks: List[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise HTTPException(status_code=413, detail=f"Page exceeds {MAX_FILE_MB} MB")
                        chunks.append(chunk)
                    return b"".join(chunks), current
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=502, detail=f"Could not fetch the page: {exc}") from exc
    raise HTTPException(status_code=502, detail=f"The page redirected more than {SCRAPER_MAX_REDIRECTS} times")


def extract_web_page_markdown(html: bytes, url: str) -> Tuple[str, str]:
    """Pull the readable part of a page out as Markdown, with its title.

    trafilatura does what a browser's reader view does - drop navigation, banners and footers,
    keep the article - and hands back Markdown, whose headings and lists the PDF and DOCX
    writers already understand (see pdf_render_lines).
    """
    import trafilatura

    try:
        decoded = html.decode("utf-8", errors="replace")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read the page: {exc}") from exc

    body = trafilatura.extract(
        decoded,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        # Formatting stays on for the headings and lists that carry the document's structure.
        # include_formatting=False would drop them to plain lines, which is the whole shape of
        # the finished PDF gone.
        include_formatting=True,
        favor_precision=True,
        url=url,
    )
    body = (body or "").strip()
    if body:
        # The reader output leaves some raw HTML behind (Wikipedia footnotes come through as
        # <sup>[1]</sup>) and carries inline bold/italic markers. Both hurt: the tags print
        # verbatim in the PDF, and the markers do not survive translation - "**Reproducible
        # builds**" came back as "*Reproduzierbare Builds**". Headings and bullets hold the
        # structure; inline emphasis is expendable.
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"(\*{1,3}|_{2})(\S.*?)\1", r"\2", body)
        body = re.sub(r"[ \t]+\n", "\n", body).strip()
    if len(body) < SCRAPER_MIN_CHARS:
        # Not just "empty": a page that redirects with JavaScript leaves a stub behind, and a
        # near-empty document would look like a translation that lost everything.
        raise HTTPException(
            status_code=422,
            detail="No readable text found on that page. It most likely builds its content or "
                   "redirects with JavaScript, which this fetcher does not run. Open the page in "
                   "your browser, save it as a PDF, and translate that instead.",
        )

    title = ""
    try:
        metadata = trafilatura.extract_metadata(decoded)
        title = (metadata.title or "").strip() if metadata else ""
    except Exception:
        title = ""
    if not title:
        title = (urlparse(url).hostname or "Website").strip()

    body = body.strip()
    # Only add the title when the extract does not already open with it as its heading.
    if not body.startswith("#") or title.lower() not in body.splitlines()[0].lower():
        body = f"# {title}\n\n{body}"
    return body, title


MARKDOWN_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)?)(.*)$")


def split_markdown_blocks(markdown: str) -> List[Tuple[str, str]]:
    """Split each line into its Markdown marker and the text after it.

    The marker has to stay out of the translation: given "## Zweiter Abschnitt" the model
    happily returns a sentence without the "##", and the heading arrives in the finished
    document as ordinary body text. Same for list bullets and quote marks.
    """
    blocks: List[Tuple[str, str]] = []
    for line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = MARKDOWN_PREFIX.match(line)
        blocks.append((match.group(1), match.group(2)) if match else ("", line))
    return blocks


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
OCR_LANGUAGE_ALIASES = {"arb": "ara", "zho": "chi_sim"}


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


def ocr_page_script(image_path: Path) -> str:
    """The script OSD sees in a page image ("Latin", "Cyrillic", …), empty when it cannot tell."""
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
        return ""
    match = OSD_SCRIPT.search(result.stdout)
    return match.group(1) if match else ""


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
    page = pymupdf.open(stream=content, filetype="pdf")[page_number - 1]
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
            printed = ocr_page_script(render_pdf_page(content, page_number, temp_dir))
    except Exception:
        return True
    return not printed or scripts_are_compatible(printed, claimed)


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
                      page_script: str, temp_dir: str) -> List[str]:
    """The script each block is printed in, one entry per block.

    OSD needs a fair amount of text before its answer means anything, and it does not say so - it
    answers anyway. Measured on the scanned fixtures: blocks of 200+ characters were right every
    time, while a 17-character Japanese heading came back "Arabic" and a 9-character one "Han".
    Reading those in the script OSD named would have replaced the heading with invented Arabic,
    so short blocks are not asked at all and take the page's script instead.
    """
    scripts = []
    for index, block in enumerate(blocks):
        script = ""
        if len(block["text"]) >= OCR_BLOCK_MIN_CHARS:
            region = render_pdf_region(content, page_number, block["box"], temp_dir, f"block{index}")
            script = ocr_page_script(region)
        if not ocr_script_is_usable(script) or scripts_are_compatible(script, page_script):
            # Either OSD named something no language can be picked for - a Chinese page had one
            # block come back "Korean", which ocr_probe_languages can only answer with English,
            # and reading Han as English deletes it - or it named a script that is written with
            # the page's anyway (kanji in a Japanese page). Neither is a reason to split.
            script = page_script
        scripts.append(script)
    return scripts


def ocr_pdf_page(content: bytes, page_number: int, source: str = AUTO_SOURCE) -> str:
    ensure_ocr_tools()
    with tempfile.TemporaryDirectory() as temp_dir:
        image_path = render_pdf_page(content, page_number, temp_dir)
        if source != AUTO_SOURCE:
            return run_tesseract(image_path, ocr_language_code(source))

        page_script = ocr_page_script(image_path)
        blocks = ocr_page_blocks(image_path, ocr_probe_languages(page_script))
        scripts = ocr_block_scripts(content, page_number, blocks, page_script, temp_dir)
        if len(set(scripts)) < 2:
            # One script on the page, which is the normal case: read it in one go. Splitting a
            # page into blocks only to read each in the same language would cost several OCR
            # runs and lose the layout analysis' view of the whole page for nothing.
            return ocr_read_with_detection(image_path, page_script)

        # A mixed page. OSD picks one script for the whole image, so reading it in one go always
        # loses the other script's text: the majority wins and the rest is either dropped or
        # reinvented in the wrong alphabet. Each block is read in its own script instead and the
        # results are put back together in tesseract's reading order.
        pieces = []
        for index, (block, script) in enumerate(zip(blocks, scripts)):
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

    markdown = "\n\n".join(pages).strip()
    if not pages_with_text:
        raise HTTPException(
            status_code=422,
            detail=f"No extractable text found. This PDF may be scanned, image-only, or protected. "
                   f"OCR ran with {ocr_language_code(source)}, but no readable text was produced.",
        )
    return markdown


MUPDF_BOLD_FLAG = 1 << 4  # span flag bit 4, per PyMuPDF's text-extraction flag table

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


def pdf_page_runs(page) -> List[Dict[str, Any]]:
    """Every text run on a PyMuPDF page, positioned in PDF user space.

    MuPDF hands over text already split into lines and spans, each with its baseline origin,
    measured bounding box, rendered size and font flags. Its own coordinates count downwards
    from the top-left of the page, so every point is mapped back through the inverse page
    transformation into the user space the overlay is later drawn in.

    Read as `rawdict` rather than `dict` for the position of every single character: a span's
    ready-made text follows the order the page paints in, which for right-to-left text is the
    producer's business and not something to rely on. Sorting the characters by their own x
    gives the order they stand in on the page, whoever wrote the file.
    """
    runs: List[Dict[str, Any]] = []
    # ponytail: O(runs^2) per page, a few hundred runs at most; index by row if a page ever
    # shows up where it isn't.
    boxes: List[Any] = []
    inverse = ~page.transformation_matrix
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
                if any(overlaps_mostly(box, taken) for taken in boxes):
                    continue
                # Recorded before the direction check, not after: an RTL run is ink on the page
                # like any other, and a duplicate layer drawn over it has to be recognised as the
                # duplicate it is. Skipping it earlier left the second copy without a partner to
                # match against, and it came through as text in its own right.
                boxes.append(box)
                runs.append({
                    "text": text,
                    "x": x,
                    "y": y,
                    "size": span["size"],
                    "width": span["bbox"][2] - span["bbox"][0],
                    "bold": bool(span["flags"] & MUPDF_BOLD_FLAG),
                    "serif": pdf_font_is_serif(span["font"]),
                    # sRGB packed into an int by MuPDF, 0 being black. Without it a title set in
                    # white on a dark cover image comes back drawn in the default black.
                    "color": span.get("color", 0),
                })
    return runs


# Runs on one baseline further apart than this (in multiples of the run's own font size) belong
# to separate cells of a table row rather than to one sentence. Wide enough to leave tab stops
# and justified word spacing inside a line alone.
PDF_CELL_GAP = 2.0


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
    inverse = ~page.transformation_matrix
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


def group_pdf_lines(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge runs that share a baseline into lines, keeping table cells apart.

    Grouping by baseline is the one grouping PDFs make reliable, which is why the layout
    pipeline builds on it instead of trying to detect blocks or columns geometrically. But a
    baseline also holds every cell of a table row, and bridging those with a space merges the
    row into one line whose translation is then reflowed across the whole table width, printed
    over the neighbouring columns.
    """
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
            if current and gap <= PDF_CELL_GAP * run["size"]:
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
            current["color_chars"][run.get("color", 0)] += length
        for cell in cells:
            cell["text"] = re.sub(r"\s+", " ", cell["text"]).strip()
            # The single point where reading order is restored: the cell has just been put
            # together from left to right, out of runs that stand in page order, so this is the
            # only place the whole visual line exists. Doing it per run instead would reverse
            # each run on its own and leave them in the wrong order relative to each other.
            if is_rtl_text(cell["text"]):
                cell["text"] = visual_to_logical(cell["text"])
            total = cell.pop("total_chars")
            cell["bold"] = cell.pop("bold_chars") * 2 > total
            cell["serif"] = cell.pop("serif_chars") * 2 > total
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


def group_pdf_paragraphs(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bundle lines into paragraphs, purely so the model gets whole sentences.

    A wrong split only costs translation quality here, never placement: every line keeps its
    own coordinates and the translation is reflowed into exactly those.
    """
    # Allowed with a bit of room over the page's own leading, so an ordinary line of the same
    # paragraph still fits while the wider gap before the next one does not.
    spacing_limit = 1.25 * typical_line_spacing(lines)
    paragraphs: List[Dict[str, Any]] = []
    for line in lines:
        current = paragraphs[-1] if paragraphs else None
        if current:
            previous = current["lines"][-1]
            spacing = previous["y"] - line["y"]
            # A paragraph's first line rarely sits where the rest of it does, and it is the second
            # line that sets the real left edge: measured against that once it exists. Until then,
            # a line further left continues an indented opening line, and a line further right
            # continues a list item, whose marker hangs out to the left of its own text.
            first = len(current["lines"]) == 1
            left = previous["x"] if first else current["lines"][1]["x"]
            aligned = (
                abs(left - line["x"]) <= 3
                or (first and 0 < previous["x"] - line["x"] <= PDF_LAYOUT_MAX_INDENT)
                or (first and PDF_LIST_MARKER.match(previous["text"])
                    and 0 < line["x"] - previous["x"] <= PDF_LAYOUT_MAX_INDENT)
            )
            fits = (
                0 < spacing <= max(1.8 * max(previous["size"], line["size"]), spacing_limit)
                and aligned
                # A marker opens an item, so it can only ever open a paragraph too. Consecutive
                # one-line items are indistinguishable from a wrapped paragraph by geometry
                # alone: same left edge, same leading, and a whole list came out as prose.
                and not PDF_LIST_MARKER.match(line["text"])
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
        document = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {exc}") from exc
    if not document.page_count:
        raise HTTPException(status_code=422, detail="PDF has no pages")

    pages = []
    for index in parse_page_range(page_range, document.page_count):
        page = document[index - 1]
        box = page.mediabox
        # Rotated pages would need the whole overlay transformed; they keep their original text.
        rotated = page.rotation % 360 != 0
        lines = [] if rotated else group_pdf_lines(pdf_page_runs(page))
        pages.append({
            "number": index,
            "width": float(box.width),
            "height": float(box.height),
            "paragraphs": group_pdf_paragraphs(lines),
            "obstacles": [] if rotated else pdf_page_obstacles(page),
        })
    if not any(page["paragraphs"] for page in pages):
        raise HTTPException(
            status_code=422,
            detail="No positioned text found. This PDF may be scanned or image-only, "
                   "translate it with Plaintext enabled instead.",
        )
    return pages


def wrap_text_to_width(text: str, width: float, size: float, bold: bool = False, serif: bool = False) -> List[str]:
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
        if current and pdf_measure_text(candidate, size, bold, serif) > width:
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
    return bool(re.search(r"[^\W\d_]{2,}", stripped))


def enclosing_box_bottom(paragraph: Dict[str, Any],
                         obstacles: Optional[List[Dict[str, float]]] = None) -> Optional[float]:
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
    return max(bottoms) if bottoms else None


def paragraph_floor(paragraph: Dict[str, Any], others: List[Dict[str, Any]],
                    obstacles: Optional[List[Dict[str, float]]] = None) -> float:
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
    box = enclosing_box_bottom(paragraph, obstacles)
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


def paragraph_width_limit(
    paragraph: Dict[str, Any], others: List[Dict[str, Any]], right_margin: float,
    obstacles: Optional[List[Dict[str, float]]] = None,
) -> float:
    """How far right the paragraph may actually run, in absolute page coordinates.

    Its own text ends where the *original* wording happened to end, which is not the width it
    had available - a heading alone on its line usually has most of the page to its right. The
    embedded font runs wider than the document's, so measuring against the original's ink makes
    almost every paragraph wrap one line early. The limit is whatever stands to its right on the
    same baselines, or the document's own right margin.

    A paragraph already running past that limit keeps its own width - a full-width heading in an
    otherwise narrower setting, see document_right_margin, or text overlapping a neighbour on the
    same baseline, which no measurement can tell from a column beside it. A box or an image is a
    wall and beats even that: an original overrunning its own table cell (Systemrequirements,
    "Graphics card") must not hand that overrun to the longer translation, which would then be
    reflowed clear across the next column.
    """
    limit = right_margin
    wall = None
    for line in paragraph["lines"]:
        for other in others:
            if other is paragraph:
                continue
            for other_line in other["lines"]:
                if abs(other_line["y"] - line["y"]) > 0.6 * max(line["size"], other_line["size"]):
                    continue
                if other_line["x"] > line["x"]:
                    limit = min(limit, other_line["x"] - 2)
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
                wall = obstacle["right"] - 2 if wall is None else min(wall, obstacle["right"] - 2)
            elif obstacle["x"] > line["x"]:
                limit = min(limit, obstacle["x"] - 2)
    limit = max(limit, max(line["right"] for line in paragraph["lines"]))
    return limit if wall is None else min(limit, wall)


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
    width_limit: Optional[float] = None,
    scale: Optional[float] = None,
    box_floor: Optional[float] = None,
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
    """
    lines = paragraph["lines"]
    left = min(line["x"] for line in lines)
    right = width_limit if width_limit is not None else max(line["right"] for line in lines)
    width = max(right - left, 10.0)
    base_size = paragraph_base_size(paragraph)
    # Same reasoning as base_size: a paragraph is drawn in whichever face most of its lines use,
    # so a bold heading stays bold instead of flattening to regular body text.
    bold = sum(1 for line in lines if line.get("bold")) * 2 > len(lines)
    serif = sum(1 for line in lines if line.get("serif")) * 2 > len(lines)
    color = Counter(line.get("color", 0) for line in lines).most_common(1)[0][0]
    if len(lines) > 1:
        leading = (lines[0]["y"] - lines[-1]["y"]) / (len(lines) - 1)
    else:
        leading = 1.2 * base_size

    def overflow_leading(size: float) -> float:
        # Overflow lines are set at the shrunken size, so spacing them at the original leading
        # pushes them further down than they need to go, straight into the next paragraph. In
        # proportion to that shrinking, not against a flat 1.2: a paragraph that did not shrink
        # at all kept its own leading everywhere except on its last line, which then sat visibly
        # tighter than the rest of the paragraph (Powerupall p. 6 "Hoffnung", p. 13).
        spacing = leading * min(1.0, size / base_size)
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
        clearance = 0.3 if floor == box_floor else 1.15
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
            return wrap_text_to_width(text, width, size, bold, serif)
        first = wrap_text_to_width(text, width - indent, size, bold, serif)[0]
        # Only when the wrap really is the text with a break put in it, which is every script
        # that separates words; CJK is wrapped character by character and joined differently.
        if not text.startswith(first):
            return wrap_text_to_width(text, width, size, bold, serif)
        rest = text[len(first):].strip()
        return [first] + (wrap_text_to_width(rest, width, size, bold, serif) if rest else [])

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
    while scale is None and not fits(len(wrapped), size) and size > base_size * PDF_LAYOUT_MIN_SCALE:
        size = max(size * 0.95, base_size * PDF_LAYOUT_MIN_SCALE)
        wrapped = wrap(size)

    # A translation into Hebrew or Arabic hangs off the right edge of the column, the way the
    # column would have been set had the document been written in that language.
    rtl = is_rtl_text(text)
    # Set flush on both edges where the original was, against the same edge the text was wrapped
    # to. Not against the original's own right edge: paragraph_width_limit sits at or past it, and
    # wrapping to the narrower one would cost lines and with them font size.
    justify = paragraph_is_justified(paragraph) and not rtl
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
            "font": {(False, False): "F1", (True, False): "F2",
                     (False, True): "F3", (True, True): "F4"}[(bold, serif)],
            "size": size,
            "line_height": 0,
            "x": (right - pdf_measure_text(wrapped_line, size, bold, serif) if rtl
                  else left + (indent if index == 0 else 0.0)),
            "y": y,
            "color": color,
        })
    if justify:
        for line in placed[:-1]:
            line["justify_to"] = right
    return placed


def redact_translated_text(content: bytes, pages: List[Dict[str, Any]], translations: List[str]) -> bytes:
    """Delete the original text of every translated paragraph from the source PDF.

    Painting boxes over it (the previous approach) left it in the file: copy/paste and Ctrl+F on
    a "translated" document still returned the original wording. MuPDF's redaction removes the
    text operators themselves, and with fill off and images/line art excluded it leaves the page
    background, table shading, icons and vector graphics untouched.
    """
    document = pymupdf.open(stream=content, filetype="pdf")
    index = 0
    for page_data in pages:
        page = document[page_data["number"] - 1]
        redacted = False
        for paragraph in page_data["paragraphs"]:
            # A paragraph without a translation keeps its original text rather than being
            # erased with nothing put in its place - as does an untranslatable fragment, whose
            # stored "translation" is just its own original text.
            if (index < len(translations) and translations[index].strip()
                    and has_translatable_text(paragraph["text"])):
                for line in paragraph["lines"]:
                    size = line["size"]
                    # Still narrower than the line's full height - a rectangle only has to touch
                    # a glyph for MuPDF to drop it, and one tall enough to hold ascenders would
                    # reach into the line above. It does reach below the baseline far enough to
                    # cover the link underlines that sit there (measured at 0.23em), which have
                    # to go with the text they underline or they end up striking through an
                    # unrelated part of the translation.
                    rectangle = pymupdf.Rect(
                        line["x"] - 1,
                        line["y"] - 0.30 * size,
                        line["right"] + 1,
                        line["y"] + 0.5 * size,
                    ) * page.transformation_matrix
                    page.add_redact_annot(rectangle, fill=False)
                    redacted = True
            index += 1
        if redacted:
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                # IF_COVERED, not NONE or IF_TOUCHED: an underline lies wholly inside its line's
                # rectangle and goes, while page backgrounds, table shading and rules extend past
                # it and stay. IF_TOUCHED would strip every panel a line of text sits on.
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
    return document.tobytes(garbage=3, deflate=True)


def render_pdf_layout_overlay(content: bytes, pages: List[Dict[str, Any]], translations: List[str]) -> bytes:
    """Stamp the translated text onto the original pages, so images, icons and vector graphics
    survive untouched."""
    overlay_pages = []
    right_margin = document_right_margin(pages)
    # Paragraphs whose reflow is rejected below have to keep their original text, which means the
    # redaction pass must not erase them either - it is driven off the same list.
    kept = list(translations)
    index = 0
    for page in pages:
        # (paragraph, translation, floor, width limit, box floor, the lines it laid out)
        reflowed: List[Tuple[Any, ...]] = []
        # The smallest scale any paragraph of a given base size needed, see below.
        scales: Dict[float, float] = {}
        for paragraph in page["paragraphs"]:
            if (index < len(translations) and translations[index].strip()
                    and has_translatable_text(paragraph["text"])):
                obstacles = page.get("obstacles") or []
                floor = paragraph_floor(paragraph, page["paragraphs"], obstacles)
                box_floor = enclosing_box_bottom(paragraph, obstacles)
                width_limit = paragraph_width_limit(
                    paragraph, page["paragraphs"], right_margin, obstacles)
                placed = reflow_paragraph(paragraph, translations[index], floor, width_limit,
                                          box_floor=box_floor)
                if len(placed) > PDF_LAYOUT_MAX_LINE_GROWTH * len(paragraph["lines"]):
                    # Not a translation of this paragraph any more. Overflow is tolerated, but a
                    # block several times its original height buries whatever sits below it, and
                    # the original is the better of the two things to be looking at.
                    kept[index] = ""
                else:
                    reflowed.append(
                        (paragraph, translations[index], floor, width_limit, box_floor, placed))
                    base = paragraph_base_size(paragraph)
                    key = round(base, 1)
                    # Clamped at the ordinary minimum: a paragraph that had to go below it is
                    # walled in by its own box (see PDF_LAYOUT_BOXED_MIN_SCALE), and one cramped
                    # table cell must not set the size of every paragraph on the page.
                    scales[key] = min(scales.get(key, 1.0),
                                      max(placed[0]["size"] / base, PDF_LAYOUT_MIN_SCALE))
            index += 1

        # Every paragraph the page sets in one size is redrawn in one size. Each shrinks itself
        # just enough to fit its own translation, which left body text at 0.7 next to body text
        # at 1.0 in the same column - the most visible flaw in the finished document. Headings
        # keep their own scale, they are a size of their own to begin with.
        lines: List[Dict[str, Any]] = []
        for paragraph, text, floor, width_limit, box_floor, placed in reflowed:
            base = paragraph_base_size(paragraph)
            scale = scales[round(base, 1)]
            if placed[0]["size"] > base * scale:
                # Only ever smaller than what this paragraph found on its own, so it still fits.
                placed = reflow_paragraph(paragraph, text, floor, width_limit, scale,
                                          box_floor=box_floor)
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
    reader = PdfReader(BytesIO(redact_translated_text(content, pages, kept)))
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


async def extract_pdf_markdown(file: UploadFile, page_range: str = "", source: str = AUTO_SOURCE) -> str:
    content = await read_upload_bytes(file, "PDF")
    return extract_pdf_markdown_from_bytes(content, file.content_type or "application/pdf", page_range, source)


def run_url_translate_job(job_id: str, url: str, source: str, target: str):
    try:
        update_job(job_id, status="running", message="Fetching page", started_at=time.time())
        html, final_url = fetch_web_page(url)
        markdown, title = extract_web_page_markdown(html, final_url)
        wait_if_paused_or_cancelled(job_id)

        if source == AUTO_SOURCE:
            source = detect_source_language(markdown)
            update_job(job_id, source=source)

        blocks = split_markdown_blocks(markdown)
        # Only lines with something to translate are sent; the markers, blank lines and any
        # wordless leftovers keep their place so the document reassembles line for line.
        translatable = [index for index, (_prefix, text) in enumerate(blocks) if has_translatable_text(text)]
        chunks = [blocks[index][1] for index in translatable]
        update_job(job_id, total=len(chunks), message=f"Translating 0 / {len(chunks)} chunks")
        translated_chunks = translate_chunks_batched(chunks, source, target, job_id, PDF_LAYOUT_BATCH_SIZE)

        lines = [prefix + text for prefix, text in blocks]
        for index, translated in zip(translatable, translated_chunks):
            prefix, _original = blocks[index]
            lines[index] = prefix + re.sub(r"\s+", " ", translated).strip()
        result = "\n".join(lines).strip()

        history_id = save_history(
            "translate-url",
            result,
            source,
            target,
            f"{history_safe_name(title)}.md",
            b"",
            # No source file: re-exporting the original HTML would mean writing the translation
            # back into it, and the reader extract's blocks do not line up with the page's own.
            "",
            {"url": final_url},
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
    except Exception as exc:
        if str(exc) == "Job stopped by user":
            update_job(job_id, status="cancelled", message="Cancelled", error=None, finished_at=time.time())
        else:
            update_job(job_id, status="failed", message="Failed", error=exception_message(exc), finished_at=time.time())


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
        paragraphs = [paragraph["text"] for page in pages for paragraph in page["paragraphs"]]
        if source == AUTO_SOURCE:
            source = detect_source_language("\n\n".join(paragraphs))
            update_job(job_id, source=source)
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
    ensure_queue_workers()
    text = "\n\n".join(str(item) for item in request.q) if isinstance(request.q, list) else str(request.q)
    job_id = create_job("translate", request.source, request.target, "Text")
    update_job(job_id, text=text)
    register_job_runner(job_id, run_text_job, (job_id, text, request.source, request.target))
    return {"job_id": job_id}


@app.post("/jobs/translate-url")
def start_translate_url_job(
    url: str = Form(...),
    source: str = Form(DEFAULT_SOURCE),
    target: str = Form(DEFAULT_TARGET),
):
    ensure_queue_workers()
    # Checked here as well as in the runner so a bad address is rejected while the user is still
    # looking at the form, instead of turning into a failed job in the queue.
    ensure_public_url(url)
    job_id = create_job("translate-url", source, target, urlparse(url.strip()).hostname or "Website")
    update_job(job_id, url=url.strip())
    register_job_runner(job_id, run_url_translate_job, (job_id, url.strip(), source, target))
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
    return {"retention_days": HISTORY_DAYS, "items": history_items()}


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
            return export_pdf_layout_with_translated_text(content, text, source_meta.get("page_range", ""))
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
async def extract_pdf(
    file: UploadFile = File(...),
    page_range: str = Form(""),
    source: str = Form(AUTO_SOURCE),
):
    return await extract_pdf_markdown(file, page_range, source)


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
    markdown = await extract_pdf_markdown(file, page_range, source)
    translated = []
    for section in re.split(r"(?m)^# Page ", markdown):
        section = section.strip()
        if not section:
            continue
        page_number, _, page_text = section.partition("\n")
        translated.append(f"# Page {page_number.strip()}\n\n{translate_text(page_text.strip(), source, target)}")
    return "\n\n".join(translated)

