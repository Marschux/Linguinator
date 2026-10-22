"""The layout-preserving PDF pipeline: runs, lines, paragraphs, reflow and the overlay."""
import re
import unicodedata
from collections import Counter
from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
from fastapi import HTTPException
# Text extraction runs on PyMuPDF (it decodes subset fonts pypdf silently drops words from and
# reports position, size and bold in one pass); pypdf still does the overlay merge.
import pymupdf
from pypdf import PdfReader, PdfWriter
from . import config, translation, textdocs, ocr, pdf_text, pdf_scan, heron


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
    # O(runs^2) per page, a few hundred runs at most; index by row if a page ever
    # shows up where it isn't.
    boxes: List[Any] = []
    inverse = ~pdf_page_flip_matrix(page)
    # Rawdict carries a dict per character, heavier than dict on long documents;
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
                # Text drawn on top of text already there: synthetic bold paints the same glyphs
                # twice ("PPoowweerr"), and some generators leave a second, offset copy of the
                # page's text behind. The first one drawn is kept and later ones covering the same
                # ink are dropped, or the page would be translated and stamped twice.
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
                        "serif": textdocs.pdf_font_is_serif(span["font"]),
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
            if mapped.width < config.PDF_IMAGE_MIN_SIZE or mapped.height < config.PDF_IMAGE_MIN_SIZE:
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
            width = run.get("width") or textdocs.pdf_measure_text(run["text"], run["size"])
            current = cells[-1] if cells else None
            gap = run["x"] - current["right"] if current else 0.0
            # A rule to write an answer on is a form field and stays a cell of its own however
            # close it sits (Powerupall item 13 leaves 9pt before its rule, the other 27 leave
            # 44pt or more). A rule drawn between two runs marks the cell boundary whatever the
            # gap: MatterhornProtokoll's Index and Fehlerbedingung columns stand 2pt apart.
            #
            # A run opening one of the page's columns starts a cell only if it is set apart by
            # half an em (more than a word space, less than the narrowest cell gap measured) and
            # follows a cell holding more than a list marker or a number. Otherwise a comma that
            # falls on a column splits a paragraph in two.
            column = (gap > 0.5 * run["size"]
                      and round(run["x"] * 2) / 2 in columns
                      and len(current["text"].strip()) > 3)
            if current and not column \
                    and not translation.FORM_RULE.fullmatch(run["text"].strip()) \
                    and not translation.FORM_RULE.fullmatch(current["text"].strip()) \
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
            cell["text"] = translation.expand_pdf_ligatures(re.sub(r"\s+", " ", cell["text"]).strip())
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
# Share of a column's lines that must end at its most common right edge, and how far that edge
# must sit past the column's left edge, before group_pdf_paragraphs looks past the immediately
# preceding paragraph. Both are needed: a column of repeated short words or page numbers reaches
# a high share while being narrow, and MatterhornProtokoll's Fehlerbedingung column is wide
# (191-349pt) but ragged. Measured (Aug 2026): real body columns sat at 86-93% share and
# 209-486pt width; table, TOC and list columns (33-42%) missed one or the other.
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


PDF_LINE_ENDS_SENTENCE = re.compile(r"[.!?:;][\"'”’)\]]*\s*$")


def group_pdf_paragraphs(lines: List[Dict[str, Any]],
                         rules: Optional[List[Dict[str, float]]] = None,
                         blocks: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """Bundle lines into paragraphs, purely so the model gets whole sentences.

    A wrong split only costs translation quality here, never placement: every line keeps its
    own coordinates and the translation is reflowed into exactly those.

    `blocks` are the layout model's text blocks (heron.page_text_blocks). Two lines that sit in
    different ones never share a paragraph: the geometry alone ran paragraphs set without a gap
    into one (MatterhornProtokoll p. 2, four into one) and a heading into the text under it
    (Stall Kamera System). Overlapping blocks are common, so lines only part when no block holds
    both; a line in none (a table cell, text in a picture) is left to the geometry. A block often
    stops short of a paragraph's last wrapped line ("…internet" / "access."), so a line that
    carries on a broken-off sentence is never parted from it (wraps_into).
    """
    def line_blocks(line: Dict[str, Any]) -> Set[int]:
        point = pymupdf.Point((line["x"] + line["right"]) / 2, line["y"] + 0.3 * line["size"])
        return {number for number, block in enumerate(blocks) if point in block}

    held = {id(line): line_blocks(line) for line in lines} if blocks else {}

    def wraps_into(current: Dict[str, Any], line: Dict[str, Any]) -> bool:
        previous = current["lines"][-1]
        if PDF_LINE_ENDS_SENTENCE.search(previous["text"]):
            return False
        if line["text"][:1].islower():
            return True
        # A German noun opens in capitals: then the line above must have run the full width.
        widest = max(own["right"] - own["x"] for own in current["lines"])
        return len(current["lines"]) > 1 and previous["right"] - previous["x"] >= 0.9 * widest

    def blocks_part(current: Dict[str, Any], line: Dict[str, Any]) -> bool:
        before, after = held.get(id(current["lines"][-1])), held.get(id(line))
        return bool(before and after and before.isdisjoint(after)) and not wraps_into(current, line)
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
            or (first and 0 < previous["x"] - line["x"] <= config.PDF_LAYOUT_MAX_INDENT)
            or (first and config.PDF_LIST_MARKER.match(previous["text"])
                and 0 < line["x"] - previous["x"] <= config.PDF_LAYOUT_MAX_INDENT)
        )
        return (
            0 < spacing <= max(size_ratio * max(previous["size"], line["size"]), spacing_limit)
            and aligned
            # A marker opens an item, so it can only ever open a paragraph too. Consecutive
            # one-line items are indistinguishable from a wrapped paragraph by geometry
            # alone: same left edge, same leading, and a whole list came out as prose.
            and not config.PDF_LIST_MARKER.match(line["text"])
            and abs(previous["size"] - line["size"]) <= 0.2 * previous["size"]
            # A change of weight ends a paragraph just as a change of size does. Not every
            # document sets its headings larger: Get_Started_With_Smallpdf sets them bold at
            # the body size, so they were swallowed by the paragraph below and lost both their
            # own weight (paragraph_base_size and the bold vote go by majority) and their own
            # alignment. Safe against a bold lead-in inside a sentence, because a line's own
            # weight is already decided by which face most of its characters use.
            and bool(previous.get("bold")) == bool(line.get("bold"))
            and not blocks_part(current, line)
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
            if 0 < x - column <= config.PDF_LAYOUT_MAX_INDENT:
                return column
        return x

    paragraphs: List[Dict[str, Any]] = []
    # The paragraph most recently extended at each x, so a paragraph split by an interleaving
    # column (see group_pdf_lines) is still found. Guarded by PDF_LAYOUT_CONTINUATION_MAX_RATIO
    # and one of two conditions:
    #
    # - both x's are pdf_layout_justified_columns (real body text): a multi-line paragraph that
    #   another column interleaves with.
    # - the candidate's column has_cell_border and the new line's row_is_partial: a table header
    #   cell wrapped to a second line. Both are required. A border alone only proves a table
    #   (Table_Across_Pages' rows are as tight as a wrapped cell), and row_is_partial alone
    #   misreads a ruleless list (Powerupall page 85's two-column checklist).
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


def extract_pdf_layout(content: bytes, page_range: str = "", layout_model: Optional[bool] = None) -> List[Dict[str, Any]]:
    """`layout_model` (default config.LAYOUT_MODEL) lets heron split paragraphs, see
    group_pdf_paragraphs. A history re-export passes what the job used: the stored translations
    are zipped back by position, so the paragraphs must come out exactly as they did then."""
    layout_model = config.LAYOUT_MODEL if layout_model is None else layout_model
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
        for index in pdf_text.parse_page_range(page_range, document.page_count):
            page = document[index - 1]
            # page.mediabox, not page.rect: pdf_page_runs/obstacles/rules extract into the page's own
            # raw, un-rotated frame (see pdf_page_flip_matrix), and the overlay is built and merged in
            # that same frame - /Rotate is left on the merged page for the viewer to apply once, to
            # both the kept original content and the overlay together.
            box = page.mediabox
            # AcroForm widgets keep their original text on a rotated page - widget.rect's
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
                    trust_text_layer = ocr.text_layer_is_trustworthy(content, index, page_text)
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
                "paragraphs": group_pdf_paragraphs(lines, rules, heron.page_text_blocks(page) if lines and layout_model else []),
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
    cjk = textdocs.detect_pdf_script(text) == "cjk"
    units = list(text) if cjk else text.split()
    separator = "" if cjk else " "

    lines: List[str] = []
    current, current_width, current_script = "", 0.0, ""
    for unit in units:
        candidate = current + separator + unit if current else unit
        # The line keeps the face its first non-Latin character picked (detect_pdf_script), so it
        # only has to be measured whole when that changes; otherwise the new word's width is added.
        # Measuring the growing line from scratch was a tenth of a layout job (Two_Column_Paper:
        # 1.6 million glyph lookups). Bold runs wider, hence the face the line is drawn in.
        script = current_script or textdocs.detect_pdf_script(unit)
        font = textdocs.script_font(bold, script, serif, italic)
        if current and script == current_script:
            width_so_far = current_width + font.text_length(separator + unit, fontsize=size)
        else:
            width_so_far = font.text_length(candidate, fontsize=size)
        if current and width_so_far > widths[min(len(lines), len(widths) - 1)]:
            lines.append(current)
            current, script = unit, textdocs.detect_pdf_script(unit)
            width_so_far = textdocs.script_font(bold, script, serif, italic).text_length(unit, fontsize=size)
        else:
            current = candidate
        current_width, current_script = width_so_far, script
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
    stripped = translation.LEADER_RUN.sub(" ", text).strip()
    if re.fullmatch(r"(https?://|www\.)\S+", stripped, re.IGNORECASE):
        return False
    if ROMAN_NUMERAL.fullmatch(stripped):
        return False
    # In a script written without spaces two characters can be a whole sentence, so the
    # two-letter bar counts those characters. A single one stays untranslated like a single
    # Latin letter: the fallback answers it with anything ("— 完 —" came back as "wieso ist das
    # alles?"), and length cannot catch that afterwards (Aug 2026, 263 short pairs: the invented
    # answer ran at x3.1, the longest genuine one at x5.0, "RAM" to "Arbeitsspeicher").
    if len(translation.CJK_DENSE_CHARS.findall(stripped)) > 1:
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
    # O(paragraphs^2) per page, fine at the few dozen a page holds; index by column if
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
    return config.PDF_LAYOUT_EDGE_MARGIN if floor is None else max(floor, config.PDF_LAYOUT_EDGE_MARGIN)


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
    edge = min(page["width"] for page in pages) - config.PDF_LAYOUT_EDGE_MARGIN
    rights = sorted(line["right"] for page in pages
                    for paragraph in page["paragraphs"] for line in paragraph["lines"])
    return min(rights[int(0.95 * (len(rights) - 1))], edge) if rights else edge


def document_left_margin(pages: List[Dict[str, Any]]) -> float:
    """The left edge the document's own text starts at, the mirror of document_right_margin."""
    edge = config.PDF_LAYOUT_EDGE_MARGIN
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
    tolerance = config.PDF_LAYOUT_CENTRE_TOLERANCE * max(right_margin - left_margin, 1.0)
    centres = []
    for line in lines:
        left_gap = line["x"] - left_margin
        right_gap = right_margin - line["right"]
        if min(left_gap, right_gap) <= config.PDF_LAYOUT_MAX_INDENT:
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
    substantial = config.PDF_LAYOUT_WALL_MIN_SPAN * max(own_right - own_left, 1)

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
                # The box the line starts in is the only wall: a shape merely standing to the
                # right says nothing about the paragraph's width.
                #
                # The text stops as far from the box's right edge as it starts from the left one
                # (Powerupall page 76 insets by 10.5pt), but only where the paragraph sits against
                # this box's left edge. Past PDF_LAYOUT_MAX_INDENT the box is a wider background,
                # like the table row behind Systemrequirements' "32 GB unified memory" cell, and
                # the flat two points apply.
                gap = own_left - obstacle["x"]
                inset = gap if 2.0 <= gap <= config.PDF_LAYOUT_MAX_INDENT else 2.0
                edge = obstacle["right"] - inset
                wall = edge if wall is None else min(wall, edge)
            elif obstacle["x"] > line["x"] and obstacle["top"] - obstacle["bottom"] >= line["size"]:
                # Only a shape at least as tall as the line stops it: the 5pt rule next to "or"
                # on Systemrequirements would otherwise set that paragraph one word per line.
                # Height and not width, because a picture narrower than the paragraph still
                # blocks it (Stall-Kamera-System). Half an em of air, since text beside a picture
                # keeps a visible gap to its frame.
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
        # A line occupies about a quarter em below its baseline and nearly a full em above.
        # Against a box's lower edge only the descenders have to clear it. Against the next
        # paragraph the gap has to read as a paragraph break: Landscape_Mixed_Pages page 3 sets
        # paragraphs 2.15x the font size apart, against 1.35x for a line within one.
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
    if not 0 < indent <= config.PDF_LAYOUT_MAX_INDENT or is_rtl_text(text):
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
    tighten_to = base_size * (config.PDF_LAYOUT_BOXED_MIN_SCALE if boxed else config.PDF_LAYOUT_TIGHTEN_SCALE)
    while scale is None and len(wrapped) > len(lines) and size > tighten_to:
        size = max(size * 0.98, tighten_to)
        wrapped = wrap(size)
    # A paragraph with a neighbour directly below may shrink past the ordinary minimum, since
    # overlapping text cannot be read at all. Only this paragraph does, the document-wide scale
    # in render_pdf_layout_overlay stays clamped. Measured (Aug 2026) on Powerupall pages 15, 50
    # and 92: four paragraphs still collided at 0.70 and needed 0.54 to 0.64.
    #
    # A CJK source is exempt: its German translation needs several times the line count, and
    # 0.51 next to a page at 0.73 (Hoshi no Kagi) looks worse than a couple of overflowing lines.
    source_text = " ".join(line["text"] for line in lines)
    crowds = floor is not None and not boxed and textdocs.detect_pdf_script(source_text) != "cjk"
    lowest = base_size * (config.PDF_LAYOUT_CROWDED_MIN_SCALE if crowds else config.PDF_LAYOUT_MIN_SCALE)
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
            "font": {value: name for name, value in textdocs.PDF_FONT_FACES.items()}[(bold, serif, italic)],
            "size": size,
            "line_height": 0,
            "x": (right_at(index) - textdocs.pdf_measure_text(wrapped_line, size, bold, serif, italic) if rtl
                  else centre - textdocs.pdf_measure_text(wrapped_line, size, bold, serif, italic) / 2
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
            if page_data.get("scan"):
                # Pixels in an image, not text: painted over in the paper's own colour. A page also
                # lands here when its text layer did not match what is printed (see
                # text_layer_is_trustworthy), and that text is deleted under the cover as well, so a
                # search of the result does not turn up the original's words.
                sample_dpi = 50
                pixmap = page.get_pixmap(dpi=sample_dpi)
                covers = []
                for paragraph in page_data["paragraphs"]:
                    if index < len(translations) and translations[index].strip():
                        for rect, box in zip(paragraph["cover"], paragraph["cover_px"]):
                            covers.append((rect, pdf_scan.scan_background(pixmap, box, sample_dpi)))
                            page.add_redact_annot(rect, fill=False)
                    index += 1
                if covers:
                    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
                for rect, colour in covers:
                    page.draw_rect(rect, color=None, fill=colour, overlay=True)
                continue
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
    # O(n²) pairwise comparison over a page's paragraphs, switch to a spatial index if a
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
            level = max(level, min(config.PDF_LAYOUT_ABSOLUTE_MIN_SIZE, bases[index]))
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
                if len(placed) > config.PDF_LAYOUT_MAX_LINE_GROWTH * len(paragraph["lines"]):
                    # Not a translation of this paragraph any more. Overflow is tolerated, but a
                    # block several times its original height buries whatever sits below it, and
                    # the original is the better of the two things to be looking at.
                    kept[index] = ""
                else:
                    reflowed.append((paragraph, text, floor, width_limit,
                                     box_floor, centre, placed))
                    if paragraph.get("own_size"):
                        index += 1
                        continue
                    base = paragraph_base_size(paragraph)
                    key = round(base, 1)
                    # Clamped at the ordinary minimum: a paragraph that had to go below it is
                    # walled in by its own box (see PDF_LAYOUT_BOXED_MIN_SCALE), and one cramped
                    # table cell must not set the size of every paragraph in the document.
                    scales[key] = min(scales.get(key, 1.0),
                                      max(placed[0]["size"] / base, config.PDF_LAYOUT_MIN_SCALE))
                    # And, separately, the smallest scale the paragraphs that had to go below that
                    # minimum needed on this page, see the second pass.
                    if placed[0]["size"] < base * config.PDF_LAYOUT_MIN_SCALE:
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

    # Every paragraph the document sets in one size is redrawn in one size. Left to itself each
    # shrinks just enough for its own translation: Powerupall's body text, 11.0 throughout, came
    # back between 7.7 and 11.0 across its 92 pages. Headings keep their own scale.
    #
    # The smallest scale the document needs is used, since it is the only one every paragraph
    # fits in. A paragraph too cramped even for that shrinks further (see
    # PDF_LAYOUT_CROWDED_MIN_SCALE); those are levelled with each other per page, so one cramped
    # table does not set the size of every cramped paragraph in the book.
    for page, reflowed, cramped in zip(pages, per_page, cramped_pages):
        bases = [paragraph_base_size(item[0]) for item in reflowed]
        # own_size "page": levelled with the page's other such paragraphs of its base size only.
        page_scales: Dict[float, float] = {}
        for (paragraph, _, _, _, _, _, placed), base in zip(reflowed, bases):
            if paragraph.get("own_size") == "page":
                key = round(base, 1)
                page_scales[key] = min(page_scales.get(key, 1.0), placed[0]["size"] / base)
        targets = []
        for (paragraph, _, _, _, _, _, placed), base in zip(reflowed, bases):
            if paragraph.get("own_size") == "page":
                targets.append(min(placed[0]["size"], base * page_scales[round(base, 1)]))
                continue
            if paragraph.get("own_size"):
                # Fits its own line and nothing else's, see scan_layout_pages.
                targets.append(placed[0]["size"])
                continue
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

    overlay_reader = PdfReader(BytesIO(textdocs.create_pdf_from_pages(overlay_pages)))
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


def export_pdf_layout_with_translated_text(content: bytes, translated_text: str, page_range: str = "",
                                           layout_model: bool = False) -> bytes:
    # Split without dropping empty blocks: translations are matched to paragraphs by position.
    blocks = [block.strip() for block in translated_text.split("\n\n")]
    return render_pdf_layout_overlay(content, extract_pdf_layout(content, page_range, layout_model), blocks)
