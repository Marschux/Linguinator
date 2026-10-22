"""Tesseract: rendering, script detection, the language probe and positioned lines."""
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from fastapi import HTTPException
# Text extraction runs on PyMuPDF (it decodes subset fonts pypdf silently drops words from and
# reports position, size and bold in one pass); pypdf still does the overlay merge.
import pymupdf
from . import langs, pdf_layout, pdf_scan


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
    if source == langs.AUTO_SOURCE:
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


# A score page's OCR is mostly staff lines and noteheads read as letters ("ne VV vv ve vy a"),
# with the sung text in between. Measured 21.10.2026 on Ruders KA, mean word confidence per line:
# sung text 65-96, noise below 62. Only a page made mostly of such lines counts as a score page:
# there they were 54-87 % of all lines with a word in them, on prose pages 12 % at most (a
# heading, a dotted contents leader), and leaving one prose line out would tear its sentence.
OCR_NOISE_MAX_CONFIDENCE = 62
OCR_NOISE_MIN_SHARE = 0.4
# Read band by band, a lyric can come out a little below that: Ruders KA p. 24's "were dif -rent
# __ I wish it were a - bout love" read at 61 once its staves were all found, and notation in the
# bands of pp. 23-30 read up to 62 too. What tells them apart there is the syllable hyphen or hold
# line (pdf_scan.SUNG_MARKS) a lyric carries and notation does not, from 55 up.
OCR_SUNG_MIN_CONFIDENCE = 55


def ocr_lines(tsv: str) -> List[Dict[str, Any]]:
    """The lines of one tesseract read with their pixel box, mean word confidence and the
    paragraph tesseract put them in; `noise` marks notation (see OCR_NOISE_MAX_CONFIDENCE),
    only ever set when the image is mostly made of it."""
    lines: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in tsv.splitlines()[1:]:
        columns = row.split("\t")
        if len(columns) >= 12 and int(columns[0]) == TSV_WORD_LEVEL and columns[11].strip():
            left, top, width, height = (int(columns[column]) for column in (6, 7, 8, 9))
            line = lines.setdefault((columns[2], columns[3], columns[4]), {
                "words": [], "confs": [], "heights": [], "boxes": [], "box": [left, top, left + width, top + height],
                "par": (int(columns[2]), int(columns[3])),
            })
            line["words"].append(columns[11].strip())
            line["heights"].append(height)
            line["boxes"].append((left, top, left + width, top + height))
            line["confs"].append(float(columns[10]))
            box = line["box"]
            box[0], box[1] = min(box[0], left), min(box[1], top)
            box[2], box[3] = max(box[2], left + width), max(box[3], top + height)
    found = [{"text": " ".join(line["words"]), "box": tuple(line["box"]), "par": line["par"],
              "words": list(zip(line["words"], line["heights"])), "boxes": line["boxes"],
              "confs": line["confs"],
              "conf": sum(line["confs"]) / len(line["confs"]), "noise": False} for line in lines.values()]
    worded = [line for line in found if pdf_layout.has_translatable_text(line["text"])]
    low = [line for line in worded if line["conf"] < OCR_NOISE_MAX_CONFIDENCE]
    if len(low) >= OCR_NOISE_MIN_SHARE * len(worded) > 0:
        for line in low:
            line["noise"] = True
    return found


def ocr_noise_lines(tsv: str) -> Set[str]:
    """The notation lines of one tesseract read, see ocr_lines."""
    return {line["text"] for line in ocr_lines(tsv) if line["noise"]}


def run_tesseract(image_path: Path, languages: str, lines: Optional[List[Dict[str, Any]]] = None,
                  psm: Optional[int] = None) -> str:
    """Plain text of an image; with `lines`, its positioned lines (ocr_lines) are added to it.
    `psm` sets tesseract's page segmentation (6: one uniform block of text)."""
    if lines is None:
        return subprocess.run(
            ["tesseract", str(image_path), "stdout", "-l", languages],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    # Text and confidences from one run, written to files: tesseract cannot put both on stdout.
    base = image_path.with_name(f"{image_path.stem}-ocr")
    subprocess.run(
        ["tesseract", str(image_path), str(base), "-l", languages]
        + (["--psm", str(psm)] if psm else []) + ["txt", "tsv"],
        check=True,
        capture_output=True,
        text=True,
    )
    lines.extend(ocr_lines(base.with_suffix(".tsv").read_text(encoding="utf-8")))
    return base.with_suffix(".txt").read_text(encoding="utf-8").strip()


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


def ocr_mean_confidence(image_path: Path, languages: str) -> float:
    """Tesseract's mean word confidence reading the image in these languages, 0 for no words."""
    lines: List[Dict[str, Any]] = []
    run_tesseract(image_path, languages, lines)
    confs = [conf for line in lines for conf in line.get("confs", [line["conf"]])]
    return sum(confs) / len(confs) if confs else 0.0


def ocr_page_script(image_path: Path) -> str:
    """The script OSD sees in a page image ("Latin", "Cyrillic", …), empty when it cannot tell.

    OSD's own confidence does not tell a right answer from a wrong one on its own. A sparse Latin
    title page came back Cyrillic, a score's notation Cyrillic at 4.44 (Ruders KA p. 26, read as
    "4 Обтед'я \"УМогуї дгеаш\""), while a real Russian scan came back Cyrillic at only 1.43. So
    a script other than Latin is checked by reading the image in it and in Latin and keeping the
    surer read (measured: p. 26 49 vs 66, Russian 96 vs 44, Greek 90 vs 49). Latin, or a script
    no language is known for, still needs OCR_SCRIPT_MIN_CONFIDENCE.
    """
    script, confidence = ocr_page_osd(image_path)
    if script and ocr_script_is_usable(script) and not scripts_are_compatible(script, "Latin"):
        # ponytail: two extra reads, only on the page that sets the document's language.
        own = ocr_mean_confidence(image_path, ocr_probe_languages(script))
        return script if own >= ocr_mean_confidence(image_path, ocr_probe_languages("Latin")) else "Latin"
    return script if confidence >= OCR_SCRIPT_MIN_CONFIDENCE else ""


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
        for internal in langs.CORE_LANGUAGES.values():
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


def ocr_read_with_detection(image_path: Path, script: str,
                            lines: Optional[List[Dict[str, Any]]] = None) -> str:
    """Read an image whose language is unknown, given the script it is printed in.

    Auto-detect on a scan is a chicken-and-egg: detection needs text, text needs OCR, OCR needs
    the language. So read once with a few languages of that script. That result is thrown away
    and only has to be good enough for langdetect; the image is then read again with the single
    language langdetect named.
    """
    probe = ocr_probe_languages(script)
    # Each read collects its own lines; only those of the read that is kept count.
    found = [] if lines is not None else None
    text = run_tesseract(image_path, probe, found)
    if not text:
        return text
    read_with = probe
    # Two reads at most on top of the probe. The probe mangles diacritics, which is enough to
    # make langdetect pick a close relative - a Czech page came back as Slovak - so the guess
    # is checked once more against the clean text the chosen language produced.
    for _ in range(2):
        code = ocr_language_code(langs.detect_source_language(text))
        if code == read_with:
            break
        candidate = [] if lines is not None else None
        better = run_tesseract(image_path, code, candidate)
        if not better:
            break
        text, read_with, found = better, code, candidate
    if lines is not None:
        lines.extend(found)
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


def ocr_pdf_page(content: bytes, page_number: int, source: str = langs.AUTO_SOURCE,
                 lines: Optional[List[Dict[str, Any]]] = None) -> str:
    """One scanned page as text; `lines` collects its positioned lines, see ocr_lines. A score
    page is read again between its staves, see read_score_bands."""
    ensure_ocr_tools()
    with tempfile.TemporaryDirectory() as temp_dir:
        image_path = render_pdf_page(content, page_number, temp_dir)
        found: Optional[List[Dict[str, Any]]] = [] if lines is not None else None
        text = read_scanned_page(content, page_number, source, image_path, temp_dir, found)
        if found and any(line["noise"] for line in found):
            language = ocr_language_code(source if source != langs.AUTO_SOURCE
                                         else langs.detect_source_language(text))
            banded = read_score_bands(image_path, found, language)
            if banded:
                text, found = banded
        if lines is not None:
            lines.extend(found)
        return text


def read_scanned_page(content: bytes, page_number: int, source: str, image_path: Path, temp_dir: str,
                      lines: Optional[List[Dict[str, Any]]]) -> str:
    """The whole-page read of ocr_pdf_page: in the job's language, or detected script by script."""
    if source != langs.AUTO_SOURCE:
        return run_tesseract(image_path, ocr_language_code(source), lines=lines)

    page_script = ocr_page_script(image_path)
    blocks = ocr_page_blocks(image_path, ocr_probe_languages(page_script))
    block_scripts = ocr_block_scripts(content, page_number, blocks, page_script, temp_dir)
    if len({script for script, _region in block_scripts}) < 2:
        # One script on the page, which is the normal case: read it in one go. Splitting a
        # page into blocks only to read each in the same language would cost several OCR
        # runs and lose the layout analysis' view of the whole page for nothing.
        return ocr_read_with_detection(image_path, page_script, lines=lines)

    # A mixed page. OSD picks one script for the whole image, so reading it in one go always
    # loses the other script's text: the majority wins and the rest is either dropped or
    # reinvented in the wrong alphabet. Each block is read in its own script instead and the
    # results are put back together in tesseract's reading order.
    pieces = []
    for index, (block, (script, region)) in enumerate(zip(blocks, block_scripts)):
        if region is None:
            region = render_pdf_region(content, page_number, block["box"], temp_dir, f"read{index}")
        region_lines: Optional[List[Dict[str, Any]]] = [] if lines is not None else None
        text = ocr_read_with_detection(region, script, lines=region_lines)
        if lines is not None:
            # The block was rendered on its own, from its padded box clamped to the page, so
            # its boxes count from there and not from the page's corner.
            dx = max(0, block["box"][0] - OCR_REGION_PADDING)
            dy = max(0, block["box"][1] - OCR_REGION_PADDING)
            for line in region_lines:
                left, top, right, bottom = line["box"]
                lines.append({**line, "box": (left + dx, top + dy, right + dx, bottom + dy),
                              "par": (index, *line["par"])})
        if text.strip():
            pieces.append(text.strip())
    return "\n\n".join(pieces)


# A staff is five lines a few pixels apart (7-9 at OCR_DPI). A line is a row covered by long dark
# runs; its threshold is relative to the page's strongest such row, since staves rarely span the
# whole page. Lines closer than STAFF_JOIN times the usual gap belong to one staff, and a group of
# STAFF_MIN_LINES or more (ledger lines and a line broken by a beam allowed for) no taller than
# STAFF_MAX_HEIGHT gaps is a staff. Measured on Ruders KA p. 24 and 30: every staff found.
STAFF_DARK = 160
STAFF_MIN_RUN = 20
STAFF_LINE_SHARE = 0.45
STAFF_JOIN = 1.5
STAFF_MIN_LINES = 4
STAFF_MAX_HEIGHT = 6


def page_gray(image_path: Path):
    """A page image as a numpy array of grey values, rows by columns."""
    import numpy as np
    pix = pymupdf.Pixmap(str(image_path))
    if pix.n > 1:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].copy()


def staff_spans(gray) -> List[Tuple[int, int]]:
    """The staves on a page (page_gray) as (top, bottom) pixel rows, top to bottom."""
    import numpy as np
    dark = gray < STAFF_DARK
    cover = np.zeros(gray.shape[0])
    for y in range(gray.shape[0]):
        edges = np.diff(np.concatenate(([0], dark[y].astype(np.int8), [0])))
        runs = np.where(edges == -1)[0] - np.where(edges == 1)[0]
        cover[y] = runs[runs >= STAFF_MIN_RUN].sum()
    # A slightly skewed line spreads over two or three rows, so they are added up. Taking the
    # largest of them lost one line of Ruders KA p. 30's CORO staff by 2 of 237, and with it that
    # staff, the piano's below and the CORO lyric between them; added up, pp. 23-30 gave 0-4 more
    # staves a page, all real, and lost none.
    cover = cover + np.roll(cover, 1) + np.roll(cover, -1)
    if not cover.any():
        return []
    rows = np.where(cover >= STAFF_LINE_SHARE * np.percentile(cover, 99.5))[0]
    lines: List[int] = []
    group: List[int] = []
    for y in rows:
        if group and y - group[-1] > 2:
            lines.append(sum(group) // len(group))
            group = []
        group.append(int(y))
    if group:
        lines.append(sum(group) // len(group))
    gaps = sorted(b - a for a, b in zip(lines, lines[1:]))
    if not gaps:
        return []
    usual = gaps[len(gaps) // 4]  # the staff gaps are the small and common ones
    spans: List[Tuple[int, int]] = []
    members = [lines[0]]
    for line in lines[1:] + [None]:
        if line is not None and line - members[-1] <= STAFF_JOIN * usual:
            members.append(line)
            continue
        if len(members) >= STAFF_MIN_LINES and members[-1] - members[0] <= STAFF_MAX_HEIGHT * usual:
            spans.append((members[0], members[-1]))
        members = [line]
    return spans


def read_score_bands(image_path: Path, lines: List[Dict[str, Any]],
                     language: str) -> Optional[Tuple[str, List[Dict[str, Any]]]]:
    """A score page read band by band between its staves; None when it has fewer than two.

    Read whole, tesseract sees text and notation as one picture: lyric lines came back in pieces
    or not at all, and staff lines as letters ("ne VV vv ve vy a"). Only whitening the staff lines
    made it worse (Ruders p. 30: lines read with confidence 11 -> 4), as tesseract then cut every
    lyric line at the gaps between its syllables. A band between two staves holds the sung text and
    the directions and nothing else, and read on its own as one block it came back whole ("but
    there is so much in the way whis-per-ing and gos-sip ..."). The title above the first staff and
    the footer below the last keep what the whole-page read found. Notation left in a band reads
    with low confidence and is marked as noise, as on the whole page.
    """
    gray = page_gray(image_path)
    staves = staff_spans(gray)
    if len(staves) < 2:
        return None
    usual = max(1, min(bottom - top for top, bottom in staves) // 4)
    first, last = staves[0][0], staves[-1][1]

    def middle(line):
        return (line["box"][1] + line["box"][3]) / 2

    outside = [line for line in lines if not first <= middle(line) <= last]
    found: List[Dict[str, Any]] = []
    texts = []
    for index, ((_, above), (below, _)) in enumerate(zip(staves, staves[1:])):
        top, bottom = above + usual // 2, below - usual // 2
        if bottom - top < 2 * usual:
            continue
        # Cut from the array: PyMuPDF has no working pixmap-crop constructor (see render_pdf_region).
        strip = gray[top:bottom]
        band_path = image_path.with_name(f"band{index}.png")
        pymupdf.Pixmap(pymupdf.csGRAY, strip.shape[1], strip.shape[0], strip.tobytes(), False).save(str(band_path))
        band_lines: List[Dict[str, Any]] = []
        text = run_tesseract(band_path, language, lines=band_lines, psm=6)
        for line in band_lines:
            left, line_top, right, line_bottom = line["box"]
            found.append({**line, "box": (left, line_top + top, right, line_bottom + top),
                          "boxes": [(x0, y0 + top, x1, y1 + top) for x0, y0, x1, y1 in line["boxes"]],
                          "par": ("band", index, *line["par"]), "band": True,
                          "noise": line["conf"] < OCR_NOISE_MAX_CONFIDENCE and not (
                              line["conf"] >= OCR_SUNG_MIN_CONFIDENCE and pdf_scan.SUNG_MARKS.search(line["text"]))})
        if text.strip():
            texts.append(text.strip())
    if not found:
        return None
    head = "\n".join(line["text"] for line in outside if middle(line) < first)
    foot = "\n".join(line["text"] for line in outside if middle(line) > last)
    return "\n\n".join(part for part in (head, *texts, foot) if part), outside + found
