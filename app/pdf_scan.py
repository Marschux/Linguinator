"""Scanned pages set as layout pages for the overlay, and the handling of score pages."""
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from langdetect import LangDetectException, detect
# Text extraction runs on PyMuPDF (it decodes subset fonts pypdf silently drops words from and
# reports position, size and bold in one pass); pypdf still does the overlay merge.
import pymupdf
from . import config, langs, translation, ocr, pdf_text, pdf_layout, heron


# Where a scanned line's glyphs sit relative to its font size: tesseract's box runs from the
# tallest glyph the line actually has to its lowest one, so a line in capitals only is shorter
# than one with ascenders and descenders, at the same size.
OCR_GLYPH_TOP = {"ascender": 0.72, "x-height": 0.5}
OCR_GLYPH_DESCENT = 0.22
OCR_DESCENDERS = set("gjpqyQ(),;[]{}")
OCR_ASCENDERS = set("bdfhklt0123456789ABCDEFGHIJKLMNOPRSTUVWXYZÄÖÜÆØÅ'\"!?/|")
OCR_COVER_PADDING = 1.5
OCR_FULL_HEIGHT = set("bdfhklABCDEFGHIJKLMNOPRSTUVWXYZÄÖÜÆØÅ0123456789")
OCR_SIZE_SNAP = 0.15
# A sung line between staves still has to be mostly letters once its syllables are joined, or it
# is notation that happened to read with some confidence: "JAN. | CE —— | z =" (55 %) came back
# as an invented "Was ist los?", while "not com - mit a - - dul - te - ry." joins to 95 %.
OCR_SUNG_MIN_LETTERS = 0.75
# The dotted leader of a contents page or cast list reads as dots or as a long run of a few
# letters ("ceessesesessesesessesss..."), and the model then translates it like a word. Measured
# on Ruders KA's cast list: in a leader the two most frequent letters make up 72-94 % of it, in
# real words of 10+ letters (German compounds, Danish OCR) 22-42 %.
OCR_LEADER_DOTS = re.compile(r"[.·…]{4,}")
OCR_LEADER_MIN_LENGTH = 10
OCR_LEADER_TOP_LETTERS = 0.6


def strip_leaders(text: str) -> str:
    def leader(word: str) -> bool:
        word = word.lower()
        top = sum(count for _, count in Counter(word).most_common(2))
        return len(word) >= OCR_LEADER_MIN_LENGTH and top / len(word) >= OCR_LEADER_TOP_LETTERS
    return " ".join(word for word in OCR_LEADER_DOTS.sub(" ", text).split() if not leader(word))


def letter_share(text: str) -> float:
    visible = [c for c in text if not c.isspace()]
    return sum(c.isalpha() for c in visible) / len(visible) if visible else 0.0


def glyph_extent(text: str) -> Tuple[float, float]:
    """How far a piece of text reaches above and below the baseline, in em: which of its glyphs
    rise (capitals, ascenders, digits) and which descend decide its box's height."""
    top = OCR_GLYPH_TOP["ascender" if any(c in OCR_ASCENDERS for c in text) else "x-height"]
    return top, OCR_GLYPH_DESCENT if any(c in OCR_DESCENDERS for c in text) else 0.0


def scan_line(line: Dict[str, Any], rect: "pymupdf.Rect", flip: "pymupdf.Matrix") -> Dict[str, Any]:
    """One scanned line in the shape extract_pdf_layout gives a text line, from its box in the
    page's raw frame. The size is the median of what each word's own box says: one box that
    reaches into the line above, or a stray mark read along with the line, would otherwise set
    the whole line in a size far off its neighbours'."""
    text = line["text"]
    px_to_pt = rect.height / max(line["box"][3] - line["box"][1], 1)
    # Only words that reach the full ascender height: "t" and "i" stop short of it, and counting
    # "want" or "it" as full height set lyrics a third smaller than the print around them.
    estimates = sorted(height * px_to_pt / sum(glyph_extent(word)) for word, height in line.get("words", [])
                       if any(c in OCR_FULL_HEIGHT for c in word)) or [rect.height / sum(glyph_extent(text))]
    size = estimates[len(estimates) // 2]
    descent = glyph_extent(text)[1]
    bottom = (pymupdf.Point(rect.x0, rect.y1) * flip).y
    return {"text": text, "x": rect.x0, "right": rect.x1, "y": bottom + descent * size, "size": size,
            "bold": False, "serif": False, "italic": False, "color": 0}


SECONDARY_LYRICS_MIN_SHARE = 0.2


def secondary_lyrics(lines: List[Dict[str, Any]], source: str, proven: Set[str]) -> Set[int]:
    """Positions of the sung lines a bilingual score prints in its other language.

    A score that carries the libretto twice (Ruders KA: English, with the Danish under it) came
    back with two near-identical German lines stacked under every system. Only the document's own
    language is translated; the other keeps its original. Each line picks among the languages the
    document has shown to hold (language_among, 15 of 15 lyric lines right on Ruders), and among
    the page's own strongest ones, so a score with no long prose to prove its second language
    still finds it.
    """
    candidates = set(proven) | {source}
    # Each line votes on its own; a language most of a fifth of them name is on the page. The page
    # read as one text only ever named whichever language had more lines.
    votes: Counter = Counter()
    sung = [join_sung_syllables(line["text"]) for line in lines if not line["noise"]]
    for text in sung:
        if len(re.findall(r"[^\W\d_]{2,}", text)) >= langs.PARAGRAPH_LANGUAGE_MIN_WORDS:
            try:
                votes[langs.CORE_LANGUAGES.get(detect(text).split("-", 1)[0])] += 1
            except LangDetectException:
                continue
    candidates |= {language for language, count in votes.items()
                   if language and count >= max(2, SECONDARY_LYRICS_MIN_SHARE * len(sung))}
    if len(candidates) < 2:
        return set()
    others = set()
    for position, line in enumerate(lines):
        if line["noise"]:
            continue
        language = langs.language_among(join_sung_syllables(line["text"]), sorted(candidates), None)
        if language and language != source:
            others.add(position)
    return others


# Tesseract reads a row of a table or diagram as one line across its columns ("Opera [gap]
# Copenhagen", "Moiras Tante [gap] alt"), and the line's box then spans rows and columns: its
# translation was set huge and in the wrong place. Measured on Ruders KA pp. 2-10: gaps between
# columns ran 2.5-46 word heights, gaps inside running text at most 1.6.
OCR_COLUMN_GAP = 2.0


def split_columns(line: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A prose line cut at every gap wider than OCR_COLUMN_GAP word heights, each piece a line
    of its own with `piece` set. Not for score pages: a held syllable leaves a long gap in a lyric."""
    boxes = line.get("boxes") or []
    if len(boxes) < 2:
        return [line]
    height = sorted(box[3] - box[1] for box in boxes)[len(boxes) // 2]
    cuts = [0] + [index for index in range(1, len(boxes))
                  if boxes[index][0] - boxes[index - 1][2] > OCR_COLUMN_GAP * height] + [len(boxes)]
    if len(cuts) == 2:
        return [line]
    pieces = []
    for start, end in zip(cuts, cuts[1:]):
        own = boxes[start:end]
        confs = line["confs"][start:end]
        pieces.append({**line, "text": " ".join(word for word, _ in line["words"][start:end]),
                       "words": line["words"][start:end], "boxes": own, "confs": confs,
                       "conf": sum(confs) / len(confs), "piece": True,
                       "box": (min(box[0] for box in own), min(box[1] for box in own),
                               max(box[2] for box in own), max(box[3] for box in own))})
    return pieces


def scan_paragraph_keys(lines: List[Dict[str, Any]], blocks: Sequence[Tuple[str, Any]]) -> List[Any]:
    """Which paragraph each line of a prose page belongs to: tesseract's own, split further by
    the layout model's blocks (heron.page_blocks, in OCR pixels).

    Tesseract ran a credits page's separate entries into one paragraph, and a whole diagram's
    labels (Ruders KA's "three time levels") into one that was then set in its top box. Lines in
    disjoint text blocks part, as in group_pdf_paragraphs, unless the upper one breaks off a
    sentence the lower carries on. A line in a picture and in no text block is a label, its own
    paragraph: heron sees the diagram as one picture with nothing inside it. So is a piece of a
    line split_columns cut up, a cell of its row.
    """
    def held(line: Dict[str, Any], labels: Set[str]) -> Set[int]:
        left, top, right, bottom = line["box"]
        point = pymupdf.Point((left + right) / 2, (top + bottom) / 2)
        return {number for number, (label, rect) in enumerate(blocks) if label in labels and point in rect}

    def width(line: Dict[str, Any]) -> int:
        return line["box"][2] - line["box"][0]

    keys: List[Any] = []
    part, current, previous_blocks = 0, [], set()
    for position, line in enumerate(lines):
        own = held(line, heron.HERON_TEXT_LABELS)
        if line.get("piece") or (not own and held(line, {"picture"})):
            keys.append(("own", position))
            continue
        if current and current[-1]["par"] == line["par"]:
            before = current[-1]
            # A German noun opens in capitals: then the line above must have run the full width.
            wraps = not pdf_layout.PDF_LINE_ENDS_SENTENCE.search(before["text"]) and (
                line["text"][:1].islower()
                or (len(current) > 1 and width(before) >= 0.9 * max(map(width, current))))
            if previous_blocks and own and previous_blocks.isdisjoint(own) and not wraps:
                part, current = part + 1, []
        else:
            current = []
        current.append(line)
        keys.append((line["par"], part))
        previous_blocks = own
    return keys


def scan_layout_pages(content: bytes, ocr_pages: Dict[int, List[Dict[str, Any]]],
                      page_range: str = "", source: Optional[str] = None,
                      layout_model: bool = False, columns: bool = False) -> List[Dict[str, Any]]:
    """A scanned document in extract_pdf_layout's shape, from the lines OCR found, so
    render_pdf_layout_overlay can set the translation where the text was printed.

    A score page goes line by line, its notation left out (see plan_pdf_page); any other page goes
    by tesseract's own paragraphs. Each paragraph also carries the boxes the original occupies,
    `cover` in the raw frame and `cover_px` in OCR pixels, for redact_translated_text to paint
    over: the text of a scan is pixels in an image, there is nothing to delete. With `source`, a
    score's lines in another language than the document's keep their original (secondary_lyrics).
    With `layout_model`, a prose page's paragraphs are split by heron's blocks
    (scan_paragraph_keys); a score page never is, heron finds hundreds of overlapping blocks there.
    With `columns`, a prose page's lines are cut at column gaps first (split_columns). Both are
    flags so a history entry re-exports with the grouping it was translated with.
    """
    scale = 72.0 / ocr.OCR_DPI
    proven: Set[str] = set()
    if source:
        # What the prose of the document proves it holds, see paragraph_language.
        for lines in ocr_pages.values():
            if not any(line["noise"] or line.get("band") for line in lines):
                proven |= {language for language in (langs.paragraph_language(" ".join(line["text"] for line in lines), None),)
                           if language}
    with pymupdf.open(stream=content, filetype="pdf") as document:
        pages = []
        for number in pdf_text.parse_page_range(page_range, document.page_count):
            page = document[number - 1]
            flip = pdf_layout.pdf_page_flip_matrix(page)
            # ponytail: assumes the crop box sits at the media box's corner, as scans do.
            to_raw = pymupdf.Matrix(scale, scale) * page.derotation_matrix
            lines = ocr_pages.get(number) or []
            score = any(line["noise"] or line.get("band") for line in lines)
            skip = secondary_lyrics(lines, source, proven) if score and source else set()
            if score:
                keys: List[Any] = list(range(len(lines)))
            else:
                if columns:
                    lines = [piece for line in lines for piece in split_columns(line)]
                scale_blocks = ocr.OCR_DPI / heron.HERON_DPI
                blocks = [(label, rect * scale_blocks) for label, rect in heron.page_blocks(page)] \
                    if layout_model and lines else []
                keys = scan_paragraph_keys(lines, blocks)
            groups: Dict[Any, List[Dict[str, Any]]] = {}
            for position, line in enumerate(lines):
                if line["noise"] or position in skip or not pdf_layout.has_translatable_text(line["text"]):
                    continue
                groups.setdefault(keys[position], []).append(line)
            paragraphs = []
            for group in groups.values():
                rects = [pymupdf.Rect(line["box"]) * to_raw for line in group]
                if score:
                    text = join_sung_syllables(group[0]["text"])
                    if letter_share(text) < OCR_SUNG_MIN_LETTERS:
                        continue
                elif sum(line["conf"] for line in group) / len(group) < ocr.OCR_NOISE_MAX_CONFIDENCE:
                    # Not text tesseract was sure of - a logo read as "we" was set back as a huge
                    # "wir" - so the original stays where it is, untouched.
                    continue
                else:
                    # A word broken at the end of a line is joined back up for the model.
                    text = re.sub(r"(?<=[^\W\d_])-\s+(?=[a-zäöüæøåß])", "", " ".join(line["text"] for line in group))
                    text = strip_leaders(text)
                if not pdf_layout.has_translatable_text(text):
                    continue
                paragraphs.append({
                    **({"band": group[0]["par"][1]} if score and group[0].get("band") else {}),
                    "text": text,
                    "lines": [scan_line(line, rect, flip) for line, rect in zip(group, rects)],
                    "cover": [rect + (-OCR_COVER_PADDING, -OCR_COVER_PADDING, OCR_COVER_PADDING, OCR_COVER_PADDING)
                              for rect in rects],
                    "cover_px": [line["box"] for line in group],
                })
            obstacles = []
            if paragraphs and not score:
                # Each size is only an estimate from the boxes, a few tenths apart for the same body
                # text, and render_pdf_layout_overlay levels sizes by their exact value: left alone,
                # paragraphs of one body text came out in visibly different sizes. Anything within
                # 15 % of the page's body size is that size; headings stand further apart.
                sizes = sorted(line["size"] for paragraph in paragraphs for line in paragraph["lines"])
                body = sizes[len(sizes) // 2]
                for paragraph in paragraphs:
                    own = sorted(line["size"] for line in paragraph["lines"])[len(paragraph["lines"]) // 2]
                    if abs(own - body) <= OCR_SIZE_SNAP * body:
                        own = body
                    for line in paragraph["lines"]:
                        line["size"] = own
            if score and paragraphs:
                # Sung lines are set in one size, the page's typical one: each size is only an
                # estimate from its own box, and lyrics a point apart look careless. And each sits
                # in a box of its own (see enclosing_box_bottom), so a longer translation shrinks
                # to its line instead of wrapping into the staff below.
                sizes = sorted(line["size"] for paragraph in paragraphs for line in paragraph["lines"])
                size = sizes[len(sizes) // 2]
                right = max(line["right"] for paragraph in paragraphs for line in paragraph["lines"])
                for paragraph in paragraphs:
                    # Music is engraved in a serif; the sung text set in a grotesk stood out from
                    # every mark around it.
                    for line in paragraph["lines"]:
                        line["serif"] = True
                    # Levelled with nothing: render_pdf_layout_overlay gives every paragraph of one
                    # size the smallest scale any of them needed in the whole document, and one long
                    # lyric anywhere then set every other line a third smaller than its print.
                    paragraph["own_size"] = True
                    for line in paragraph["lines"]:
                        line["size"] = size
                        obstacles.append({"x": line["x"] - 1, "right": right, "top": line["y"] + size,
                                          "bottom": line["y"] - OCR_GLYPH_DESCENT * size - 0.5})
            pages.append({
                "number": number, "width": float(page.mediabox.width), "height": float(page.mediabox.height),
                "paragraphs": paragraphs, "obstacles": obstacles, "columns": [], "row_rules": [], "widgets": [],
                "scan": True,
            })
        return pages


SUNG_SENTENCE_END = re.compile(r"[.!?;:][\"')\]]*$")
SUNG_LYRIC_MIN_WORDS = 3


def sung_sentences(pages: List[Dict[str, Any]]) -> List[List[int]]:
    """The scan's paragraphs (flattened over pages) grouped into what is translated together.

    A score is set line by line, and one sung sentence runs over several of them: one printed line
    read as two pieces, and a voice continuing in the next system. Translated line by line, the
    model saw "…love or sunsets or" and "happiness or snow." apart. Pieces of a band's row are
    joined left to right; a row continues the row in the same place (same index among as many
    rows) of the voice's next band, over a page break too. A line joins the one before only while
    that one has not ended its sentence and it opens in lower case, which keeps a capitalised
    direction ("Allegro", "Slowly") out. Everything else stays a group of one.
    """
    groups: List[List[int]] = []
    open_by_voice: Dict[Tuple[int, int], List[int]] = {}
    texts: List[str] = []
    index = 0
    for page in pages:
        bands: Dict[int, List[Tuple[int, Dict[str, Any]]]] = {}
        for paragraph in page["paragraphs"]:
            texts.append(paragraph["text"])
            if "band" in paragraph:
                bands.setdefault(paragraph["band"], []).append((index, paragraph))
            else:
                groups.append([index])
            index += 1
        if not bands:
            open_by_voice = {}
        for band in sorted(bands):
            rows: List[List[Tuple[int, Dict[str, Any]]]] = []
            for item in sorted(bands[band], key=lambda item: item[1]["cover_px"][0][1]):
                top, bottom = item[1]["cover_px"][0][1], item[1]["cover_px"][0][3]
                first = rows[-1][0][1]["cover_px"][0] if rows else None
                if first and first[1] <= (top + bottom) / 2 <= first[3]:
                    rows[-1].append(item)
                else:
                    rows.append([item])
            sung: Dict[Tuple[int, int], List[int]] = {}
            for number, row in enumerate(rows):
                voice = (number, len(rows))
                group = open_by_voice.get(voice)
                for position, _ in sorted(row, key=lambda item: item[1]["cover_px"][0][0]):
                    text = texts[position]
                    if len(text.split()) < SUNG_LYRIC_MIN_WORDS and not text[:1].islower():
                        # A direction, or too little to tell: on its own, the voices left as they were.
                        groups.append([position])
                        continue
                    if not (group and not SUNG_SENTENCE_END.search(texts[group[-1]]) and text[:1].islower()
                            and sum(len(texts[i]) for i in group) + len(text) < config.MAX_CHARS):
                        group = [position]
                        groups.append(group)
                    else:
                        group.append(position)
                    sung[voice] = group
            # A band with sung text in it is the voices' next system; one they are missing from ends them.
            if sung:
                open_by_voice = sung
    return sorted(groups)


def spread_words(translation: str, originals: Sequence[str]) -> List[str]:
    """A translation shared out over the lines its original came from, by each line's share of the
    original's words, every line keeping at least one word while there are enough."""
    words = translation.split()
    counts = [max(1, len(original.split())) for original in originals]
    total, done, parts, start = sum(counts), 0, [], 0
    for position, count in enumerate(counts):
        done += count
        left = len(counts) - position - 1
        end = min(max(round(len(words) * done / total), start + 1), len(words) - left) if left else len(words)
        end = min(max(end, start + 1), len(words))
        parts.append(" ".join(words[start:end]))
        start = end
    return parts


def scan_background(pixmap: "pymupdf.Pixmap", box: Sequence[int], dpi: int) -> Tuple[float, float, float]:
    """The paper colour around a box of OCR pixels, so the cover blends into a yellowed or grey
    scan instead of standing out white: a bright value from a ring just outside it, since staff
    lines and note stems run through that ring too and the median came out grey."""
    factor = dpi / ocr.OCR_DPI
    left, top, right, bottom = (int(round(value * factor)) for value in box)
    ring = []
    for x in range(left - 2, right + 3):
        for y in (top - 2, bottom + 2):
            if 0 <= x < pixmap.width and 0 <= y < pixmap.height:
                ring.append(pixmap.pixel(x, y))
    for y in range(top - 2, bottom + 3):
        for x in (left - 2, right + 2):
            if 0 <= x < pixmap.width and 0 <= y < pixmap.height:
                ring.append(pixmap.pixel(x, y))
    if not ring:
        return (1.0, 1.0, 1.0)
    channels = list(zip(*ring))[:3]
    return tuple(sorted(channel)[int(len(channel) * 0.9)] / 255 for channel in channels)


def export_scan_with_translated_text(content: bytes, translated_text: str, page_range: str = "",
                                     source: str = langs.AUTO_SOURCE, language: Optional[str] = None,
                                     layout_model: bool = False, columns: bool = False) -> bytes:
    """The history re-export of a scan overlay: read the pages again and zip the stored
    translations back on by position, as export_pdf_layout_with_translated_text does."""
    ocr_pages: Dict[int, List[Dict[str, Any]]] = {}
    pdf_text.extract_pdf_markdown_from_bytes(content, "application/pdf", page_range, source, ocr_pages)
    blocks = [block.strip() for block in translated_text.split("\n\n")]
    return pdf_layout.render_pdf_layout_overlay(
        content, scan_layout_pages(content, ocr_pages, page_range, language, layout_model, columns), blocks)


def is_score_page(text: str, noise: Optional[Set[str]]) -> bool:
    """Whether OCR found notation lines on this page (ocr_noise_lines)."""
    return bool(noise) and any(line.strip() in noise for line in text.splitlines())


# Sung text is printed in syllables under the notes ("be - lieve", "hap-pi - ness", "a - - dul -
# te - ry"); the model takes each piece for a word ("lyk-ke el-ler sne" came back
# "Glück-Kee-El-Schnee"). "_" runs are the lines a held syllable is drawn out with.
SUNG_SYLLABLE_BREAK = re.compile(r"(?<=[^\W\d_])\s*-[\s-]*(?=[^\W\d_])")
SUNG_EXTENSION = re.compile(r"\s*_+\s*")


def join_sung_syllables(line: str) -> str:
    return SUNG_SYLLABLE_BREAK.sub("", SUNG_EXTENSION.sub(" ", line)).strip()


def plan_pdf_page(text: str, noise: Optional[Set[str]]) -> List[Tuple[str, bool]]:
    """A page's pieces and whether each is to be translated.

    A score page goes line by line: its notation lines keep their original and only the sung
    text and directions between them reach the model. Sent as a whole, the notation came back
    as invented words wrapped around the lyrics, and took most of the job's time (Ruders KA:
    4 h estimated for 50 pages).
    """
    if not is_score_page(text, noise):
        # Paragraph by paragraph, so each gets its own language (language_groups) and the
        # translation keeps the page's paragraph breaks.
        return [(chunk, pdf_layout.has_translatable_text(chunk))
                for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()
                for chunk in translation.split_long_text(paragraph.strip(), config.MAX_CHARS)]
    pieces = []
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if line in noise or not pdf_layout.has_translatable_text(line):
            pieces.append((line, False))
        else:
            pieces.append((join_sung_syllables(line), True))
    return pieces
