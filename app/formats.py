"""Extraction and re-export for the office and text formats (DOCX, ODT, PPTX, CSV/XLSX, HTML, subtitles, JSON/YAML, PO, XLIFF)."""
import re
import csv
import json
import zipfile
from html.parser import HTMLParser
from io import BytesIO, StringIO
from typing import Any, Dict, List, Set, Tuple
# lxml, not the stdlib xml.etree.ElementTree it's a drop-in replacement for here: a real
# Office-authored file's mc:Ignorable="x14ac xr xr2 xr3" names namespace prefixes by that literal
# string, and stdlib ElementTree renames every prefix to its own ns0/ns1/... on serialization,
# leaving mc:Ignorable pointing at prefixes that no longer exist - Excel refuses to open the
# result ("file is damaged"), measured on a real spreadsheet round-tripped through export. lxml
# preserves the original prefixes, so the reference stays valid. lxml.etree.ParseError is a base
# class of its XMLSyntaxError, so every existing `except ElementTree.ParseError` still catches it.
from lxml import etree as ElementTree
from fastapi import HTTPException
from . import config, pdf_text


ElementTree.register_namespace("w", "http://schemas.openxmlformats.org/wordprocessingml/2006/main")
ElementTree.register_namespace("office", "urn:oasis:names:tc:opendocument:xmlns:office:1.0")
ElementTree.register_namespace("text", "urn:oasis:names:tc:opendocument:xmlns:text:1.0")
ElementTree.register_namespace("s", "http://schemas.openxmlformats.org/spreadsheetml/2006/main")
ElementTree.register_namespace("a", "http://schemas.openxmlformats.org/drawingml/2006/main")
ElementTree.register_namespace("xlf", "urn:oasis:names:tc:xliff:document:1.2")
ElementTree.register_namespace("style", "urn:oasis:names:tc:opendocument:xmlns:style:1.0")
ElementTree.register_namespace("fo", "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0")


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
    selected = set(pdf_text.parse_page_range(page_range, total_pages))
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
    if uncompressed_size > config.MAX_ZIP_UNCOMPRESSED_BYTES:
        raise HTTPException(status_code=413, detail=f"Archive expands beyond {config.MAX_ZIP_UNCOMPRESSED_BYTES // 1024 // 1024} MB")


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
    selected = set(pdf_text.parse_page_range(page_range, total_pages))
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
