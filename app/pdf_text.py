"""A PDF as Markdown, page by page, with OCR for pages that need it."""
import re
from typing import Any, Dict, List, Optional
from fastapi import HTTPException
# Text extraction runs on PyMuPDF (it decodes subset fonts pypdf silently drops words from and
# reports position, size and bold in one pass); pypdf still does the overlay merge.
import pymupdf
from . import config, langs, ocr, pdf_layout


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


def extract_pdf_markdown_from_bytes(
    content: bytes,
    content_type: str = "application/pdf",
    page_range: str = "",
    source: str = langs.AUTO_SOURCE,
    ocr_pages: Optional[Dict[int, List[Dict[str, Any]]]] = None,
) -> str:
    """The PDF as Markdown, one "# Page N" section per page; `ocr_pages` collects the positioned
    lines of every page read by OCR (ocr_lines), by page number."""
    if content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")

    if len(content) > config.MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"PDF exceeds {config.MAX_FILE_MB} MB")

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
        auto = source == langs.AUTO_SOURCE
        for index in selected_pages:
            text = document[index - 1].get_text() or ""
            text = re.sub(r"[ \t]+\n", "\n", text).strip()
            needs_ocr = not text or len(text) < config.PDF_LOW_TEXT_CHARS
            if not needs_ocr:
                if trust_text_layer is None:
                    trust_text_layer = ocr.text_layer_is_trustworthy(content, index, text)
                needs_ocr = not trust_text_layer
            if needs_ocr:
                page_lines: List[Dict[str, Any]] = []
                ocr_text = ocr.ocr_pdf_page(content, index, source, lines=page_lines)
                if ocr_text and auto:
                    if source == langs.AUTO_SOURCE:
                        # Keep what the first scanned page turned out to be: the pages after it then
                        # read in one pass instead of probing the same document over and over.
                        source = langs.detect_source_language(ocr_text)
                    else:
                        # Unless a page is clearly in another language: Ruders KA's Danish synopsis,
                        # read with English traineddata, lost every æ/ø/å ("fgdselsrate").
                        own = langs.paragraph_language(ocr_text, source)
                        if own != source:
                            reread_lines: List[Dict[str, Any]] = []
                            reread = ocr.ocr_pdf_page(content, index, own, lines=reread_lines)
                            if reread:
                                ocr_text, page_lines = reread, reread_lines
                if ocr_text:
                    text = ocr_text
                    if ocr_pages is not None:
                        ocr_pages[index] = page_lines

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
                   f"OCR ran with {ocr.ocr_language_code(source)}, but no readable text was produced.",
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
            images = pdf_layout.pdf_page_images(page)
            if images:
                info[str(index + 1)] = {
                    "images": images,
                    "width": float(page.rect.width),
                    "height": float(page.rect.height),
                }
        return info
    finally:
        document.close()
