"""Fonts, and plain-text PDF and DOCX documents built from a translation."""
import re
import zipfile
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
# Text extraction runs on PyMuPDF (it decodes subset fonts pypdf silently drops words from and
# reports position, size and bold in one pass); pypdf still does the overlay merge.
import pymupdf
from . import config, pdf_text, pdf_layout, pipeline


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


def wrap_pdf_line(text: str, size: float = config.PDF_FONT_SIZE) -> List[str]:
    # Measured by actual glyph width (script-aware, see detect_pdf_script), not a fixed character
    # count: CJK glyphs run close to twice as wide as Latin ones at the same point size, so a
    # char-count cap tuned for Latin text ran CJK lines off the page edge.
    if not text:
        return [""]
    return pdf_layout.wrap_text_to_width(text, config.PDF_PAGE_WIDTH - 2 * config.PDF_MARGIN, size)


def markdown_page_sections(text: str) -> List[Dict[str, str]]:
    sections = pipeline.pdf_sections(text)
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
                for line in wrap_pdf_line(heading, config.PDF_HEADING_FONT_SIZE):
                    lines.append({"text": line, "font": "F2", "size": config.PDF_HEADING_FONT_SIZE, "line_height": 18})
                lines.append({"text": "", "font": "F1", "size": config.PDF_FONT_SIZE, "line_height": 8})
                continue
        wrapped = wrap_pdf_line(stripped)
        for line in wrapped:
            lines.append({"text": line, "font": "F1", "size": config.PDF_FONT_SIZE, "line_height": config.PDF_LINE_HEIGHT})
    return lines


def paginate_pdf_lines(lines: List[Dict[str, Any]],
                       usable_height: float = config.PDF_PAGE_HEIGHT - (2 * config.PDF_MARGIN) - 26
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
    return width - occupied < config.PDF_IMAGE_MIN_TEXT_BAND


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
        if usable < config.PDF_IMAGE_MIN_TEXT_BAND:
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
                for line in wrap_pdf_line(heading, config.PDF_HEADING_FONT_SIZE):
                    lines.append({"text": line, "font": "F2", "size": config.PDF_HEADING_FONT_SIZE,
                                 "line_height": 18, "x": margin})
                    y -= 18
                lines.append({"text": "", "font": "F1", "size": config.PDF_FONT_SIZE, "line_height": 8,
                             "x": margin})
                y -= 8
                continue
        bands = image_line_bands(images, width, margin, y, config.PDF_LINE_HEIGHT)
        wrapped = pdf_layout.wrap_text_to_width(stripped, [band[1] for band in bands], config.PDF_FONT_SIZE)
        for index, line in enumerate(wrapped):
            lines.append({"text": line, "font": "F1", "size": config.PDF_FONT_SIZE,
                         "line_height": config.PDF_LINE_HEIGHT,
                         "x": bands[min(index, len(bands) - 1)][0]})
            y -= config.PDF_LINE_HEIGHT
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
                     if not pdf_image_blocks_width(image, info["width"] - 2 * config.PDF_MARGIN, config.PDF_MARGIN)]
        if images:
            width, height = info["width"], info["height"]
            margin = config.PDF_MARGIN
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
    if pdf_layout.is_rtl_text(text):
        # MuPDF's own right_to_left turns the whole string around, a year or a Latin name inside
        # the line with it. So the line is handed over a stretch at a time, laid out from its
        # right edge leftwards, each stretch written in the direction it actually runs.
        cursor = x
        space = font.text_length(" ", fontsize=size)
        for part, rtl in reversed(pdf_layout.direction_segments(text)):
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
        if not space < gap <= config.PDF_JUSTIFY_MAX_SPACE * space:
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
            width = page_data.get("width", config.PDF_PAGE_WIDTH)
            height = page_data.get("height", config.PDF_PAGE_HEIGHT)
            margin = page_data.get("margin", config.PDF_MARGIN)
            document.new_page(width=width, height=height)
            index = output_page_number - 1
            for image in page_data.get("images") or []:
                images.append((index, image, height))
            y = height - margin
            if page_data["source_page"]:
                heading = "Page " + page_data["source_page"]
                if page_data["continuation"]:
                    heading += " continued"
                lines.append((index, heading, margin, y, "F2", config.PDF_HEADING_FONT_SIZE, 0, height, None))
                y -= 24
            for line in page_data["lines"]:
                if line["text"]:
                    lines.append((index, line["text"], line.get("x", margin), line.get("y", y),
                                  line["font"], line["size"], line.get("color", 0), height,
                                  line.get("justify_to")))
                y -= line["line_height"]
            if page_data.get("footer", True):
                lines.append((index, f"{output_page_number}", width - margin, margin // 2,
                              "F1", config.PDF_FOOTER_FONT_SIZE, 0, height, None))

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
    page_images = pdf_text.extract_pdf_page_images(source_content) if source_content else None
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
