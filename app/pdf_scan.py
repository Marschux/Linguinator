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
# i and j too: their dots reach about as high, and "non-singing", sized as if it stopped at the
# x-height, came out a third larger than the cast list around it (Ruders KA p. 6).
OCR_ASCENDERS = set("bdfhklt0123456789ABCDEFGHIJKLMNOPRSTUVWXYZÄÖÜÆØÅ'\"!?/|ij")
OCR_COVER_PADDING = 1.5
OCR_FULL_HEIGHT = set("bdfhklABCDEFGHIJKLMNOPRSTUVWXYZÆ0123456789")
# Capitals with a mark above or a stroke through reach past the others: "ØJNE" read 22px where
# the capitals around it read 18, and Ruders KA p. 7's line came out a fifth larger than its list.
OCR_OVERSIZE = set("ÄÖÜØÅÀÁÂÈÉÊÌÍÎÒÓÔÙÚÛÇÑ")
OCR_SIZE_SNAP = 0.15
# A sung line between staves still has to be mostly letters once its syllables are joined, or it
# is notation that happened to read with some confidence: "JAN. | CE —— | z =" (55 %) came back
# as an invented "Was ist los?", while "not com - mit a - - dul - te - ry." joins to 95 %.
OCR_SUNG_MIN_LETTERS = 0.75
# A score line also needs one word of three letters: instrument cues and dynamics ("Hn. SP",
# "d Sf") read with confidence too, came back "translated", and their cover cut into the chord
# beside them (Ruders KA p. 27).
OCR_SCORE_WORD = re.compile(r"[^\W\d_]{3,}")
# A single word in capitals on a score is a part's name ("OFFRED", "HANDMAIDS", "CORO"): "OFFRED"
# came back "AUSFRAGE".
OCR_SCORE_NAME = re.compile(r"[^\W\d_a-zäöüæøåß]{3,}[.:]?")
# The dotted leader of a contents page or cast list reads as dots or as a long run of a few
# letters ("ceessesesessesesessesss..."), and the model then translates it like a word. Measured
# on Ruders KA's cast list: in a leader the two most frequent letters make up 72-94 % of it, in
# real words of 10+ letters (German compounds, Danish OCR) 22-42 %, but Danish "hængningen" (60 %)
# and "medlemmerne" (64 %) were taken for leaders at 0.6 and dropped from the synopsis (p. 9).
OCR_LEADER_DOTS = re.compile(r"[.·…]{4,}")
OCR_LEADER_MIN_LENGTH = 12
OCR_LEADER_TOP_LETTERS = 0.7


GLUED_LEADER = re.compile(r"[.·…]{2,}(?=[^\W\d_])")
TRAILING_DOTS = re.compile(r"(?<=[^.·…])[.·…]{2,}$")
SHORT_LEADER = re.compile(r"[^\W\d_]{1,2}[.·…]{2,}")


def is_leader(word: str) -> bool:
    """A dotted leader read as a word: dots only ("..", "........"), or a long run of a few letters
    (see strip_leaders). Not three dots: that is an ellipsis, and prose opens with one ("... She is
    torn from her husband", Ruders KA p. 8) - unless it follows a word ("midtwenties) ... mezzo
    soprano"), see column_spans."""
    dots = not word.strip(".·…")
    return (dots and len(word) >= 2) or bool(OCR_LEADER_DOTS.search(word)) or not strip_leaders(word)


def strip_leaders(text: str) -> str:
    text = " ".join(GLUED_LEADER.sub("", word) for word in text.split())

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
                       if any(c in OCR_FULL_HEIGHT for c in word) and not OCR_OVERSIZE & set(word)) or [rect.height / sum(glyph_extent(text))]
    size = estimates[len(estimates) // 2]
    descent = glyph_extent(text)[1]
    bottom = (pymupdf.Point(rect.x0, rect.y1) * flip).y
    return {"text": text, "x": rect.x0, "right": rect.x1, "y": bottom + descent * size, "size": size,
            "bold": False, "serif": False, "italic": bool(line.get("italic")), "color": 0}


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
# Unless the gap holds ink: a word misread or left out leaves one as wide ("year-ph [gap] )" for
# "year-plus-six)", 17 % ink), where column gaps held 6 % at most, dotted leaders included.
OCR_GAP_MAX_INK = 0.1


def column_spans(line: Dict[str, Any], leaders: bool = False) -> List[range]:
    """The word ranges of a line between gaps wider than OCR_COLUMN_GAP word heights."""
    boxes = line.get("boxes") or []
    if len(boxes) < 2:
        return [range(len(line.get("words") or []))]
    height = sorted(box[3] - box[1] for box in boxes)[len(boxes) // 2]
    ink = line.get("gap_ink") or [0.0] * len(boxes)
    cuts = {index for index in range(1, len(boxes))
            if boxes[index][0] - boxes[index - 1][2] > OCR_COLUMN_GAP * height and ink[index] <= OCR_GAP_MAX_INK}
    if leaders:
        # A dotted leader read as a word fills the gap between a name and its part: it is the gap.
        cuts |= {cut for index, (word, _) in enumerate(line["words"])
                 if is_leader(word) and (index or word not in ("...", "…"))
                 for cut in (index, index + 1) if 0 < cut < len(boxes)}
        # Or it is read stuck to the front of the part ("…..mezzo sopran", Ruders KA p. 7), or as a
        # letter or two and dots, unsure ("Eye and Guard 1 ou...", p. 6; a sure "so..." is prose).
        cuts |= {index for index, (word, _) in enumerate(line["words"]) if index and GLUED_LEADER.match(word)}
        confs = line.get("confs") or [100.0] * len(boxes)
        cuts |= {cut for index, (word, _) in enumerate(line["words"])
                 if index and SHORT_LEADER.fullmatch(word) and confs[index] < ocr.OCR_NOISE_MAX_CONFIDENCE
                 for cut in (index, index + 1) if cut < len(boxes)}
    cuts = [0] + sorted(cuts) + [len(boxes)]
    return [range(start, end) for start, end in zip(cuts, cuts[1:])]


def sub_line(line: Dict[str, Any], words: Sequence[int], **extra: Any) -> Dict[str, Any]:
    """The line made of only these of its words."""
    own = [line["boxes"][index] for index in words]
    confs = [line["confs"][index] for index in words]
    extra = {**{key: [line[key][index] for index in words] for key in ("covers", "gap_ink") if key in line},
             **extra}
    return {**line, "text": " ".join(line["words"][index][0] for index in words),
            "words": [line["words"][index] for index in words], "boxes": own, "confs": confs,
            "conf": sum(confs) / len(confs), **extra,
            "box": (min(box[0] for box in own), min(box[1] for box in own),
                    max(box[2] for box in own), max(box[3] for box in own))}


def cover_box(line: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """The pixels a line's original occupies, its words' underlines included."""
    covers = line.get("covers")
    if not covers:
        return tuple(line["box"])
    return (line["box"][0], line["box"][1], line["box"][2], max(line["box"][3], max(box[3] for box in covers)))


def split_columns(line: Dict[str, Any], leaders: bool = False) -> List[Dict[str, Any]]:
    """A prose line cut at every column gap (column_spans), each piece a line of its own with
    `piece` set. Not for score pages: a held syllable leaves a long gap in a lyric."""
    spans = column_spans(line, leaders)
    if len(spans) < 2:
        return [line]
    return [sub_line(line, span, piece=True) for span in spans]


# A stage direction shares a score's bands with the sung text, often a line too ("i stil - le
# skov, —___ [gap] off stage right. The Double, stage centre, ..."), and the two went to the model
# as one sentence: "Stilles Holz, bewaffnete Wachen betreten ...". The gap cannot tell them apart,
# notes space a lyric's syllables up to 30 word heights (Ruders KA pp. 23-30). The text can: a
# lyric is broken into syllables ("stil - le", "whis-per-ing") or held ("__") every few words, a
# direction is not. Between two letters only: OCR reads stray rules as dashes ("——- Pe").
OCR_DIRECTION_MIN_WORDS = 5
SUNG_MARKS = re.compile(r"[^\W\d_]\s*-\s*[^\W\d_]|_")


def is_direction(text: str) -> bool:
    return len(re.findall(r"[^\W\d_]{2,}", text)) >= OCR_DIRECTION_MIN_WORDS and not SUNG_MARKS.search(text)


# Measured on Ruders KA p. 23: "7 8 ee" read at 47, 38 and 24, the sung words beside them at 68-97.
OCR_SCORE_JUNK_CONFIDENCE = 50
SUNG_HOLD = re.compile(r"[-_—–]+")


def is_score_junk(word: str, conf: float) -> bool:
    """A mark tesseract read off the notation of a score: no letter in it (a syllable's hyphen or
    hold line aside), or a letter or two read with less than OCR_SCORE_JUNK_CONFIDENCE."""
    letters = sum(c.isalpha() for c in word)
    if not letters:
        return not SUNG_HOLD.fullmatch(word)
    return letters <= 2 and conf < OCR_SCORE_JUNK_CONFIDENCE


def trim_score_line(line: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A score line without the notation tesseract read at either end of it ("7 8 ee I'm sor-ry",
    "| want you", Ruders KA pp. 23-24): marks with no letter (a syllable's hyphen or hold line
    stays), and a letter or two read with less than OCR_SCORE_JUNK_CONFIDENCE. A longer word read
    unsure is still a sung one ("story", "unto"). The line had reached out to the margin, and its
    translation was set there. Empty when nothing is left; a noise line, or one without word
    boxes, as it is."""
    if line["noise"] or not line.get("boxes"):
        return [line]

    def junk(index: int) -> bool:
        return is_score_junk(line["words"][index][0], line["confs"][index])

    start, end = 0, len(line["words"])
    while start < end and junk(start):
        start += 1
    while end > start and junk(end - 1):
        end -= 1
    if (start, end) == (0, len(line["words"])):
        return [line]
    # Whether it reads as text at all is still judged on the whole line (OCR_SUNG_MIN_LETTERS):
    # "i feo | eae | |" is notation, with its bars trimmed off it would pass.
    return [sub_line(line, range(start, end), untrimmed=line["text"])] if end > start else []


def split_directions(line: Dict[str, Any], numbers: bool = True) -> List[Dict[str, Any]]:
    """A score line as its sung part (one line, as before) and its directions (`direction` set).
    The sung text stands left of the directions, so what follows one ("initially offstage.") is
    direction too, however short."""
    if not line.get("boxes"):
        return [line]
    spans = column_spans(line)
    first = next((number for number, span in enumerate(spans)
                  if is_direction(" ".join(line["words"][i][0] for i in span))), None)
    if first is None:
        return [line]
    # A bar or rehearsal number set right before a direction ("7 hold Offred, ...", Ruders KA
    # p. 27) is no part of it: a piece of its own, which as a bare number keeps its original.
    start = spans[first].start
    while numbers and start < spans[first].stop - 1 and line["words"][start][0].isdigit():
        start += 1
    sung = [index for span in spans[:first] for index in span]
    number = [sub_line(line, range(spans[first].start, start))] if start > spans[first].start else []
    spans = spans[:first] + [range(start, spans[first].stop)] + spans[first + 1:]

    def trimmed(span: range) -> range:
        # A cue read into the end of a direction ("unobtrusively ——- Pe", where "Pe" is the
        # "Cl./Vl./Va." above the bar, Ruders KA p. 27) is no part of it, and is not painted over.
        stop = span.stop
        while numbers and stop - 1 > span.start and is_score_junk(line["words"][stop - 1][0], line["confs"][stop - 1]) \
                and any(c.isalpha() for c in line["words"][stop - 1][0]):
            stop -= 1
        return range(span.start, stop)

    return (([sub_line(line, sung)] if sung else []) + number
            + [sub_line(line, trimmed(span), direction=True) for span in spans[first:]])


def direction_groups(lines: List[Dict[str, Any]]) -> Dict[int, int]:
    """The direction lines of a score page, each mapped to the first line of the direction it
    belongs to: one carries on the one just above it in the same band where the two overlap side
    to side, as a direction is set over several lines, two side by side in places (Ruders KA
    p. 27). They are translated together and set line by line (sung_sentences)."""
    groups: Dict[int, int] = {}
    open_ends: Dict[int, Dict[str, Any]] = {}
    for position, line in enumerate(lines):
        if not line.get("direction"):
            continue
        left, top, right, bottom = line["box"]
        for first, last in open_ends.items():
            if (last["par"][:2] == line["par"][:2] and last["box"][0] < right and left < last["box"][2]
                    and -0.5 * (bottom - top) <= top - last["box"][3] <= 1.5 * (bottom - top)):
                groups[position] = first
                open_ends[first] = line
                break
        else:
            groups[position] = position
            open_ends[position] = line
    return groups


# Which grouping of a scan's lines a history entry was translated with, so its re-export zips the
# translations onto the same paragraphs: 1 cuts prose lines at column gaps, 2 also takes the
# directions out of a score's sung lines, 3 keeps a gap with ink in it whole (OCR_GAP_MAX_INK) and a
# number before a direction apart from it, 4 cuts at dotted leaders, sets each row of a cast list
# apart (is_row) and trims notation off the ends of a score's lines.
SCAN_GROUPING = 4


# A cast list sets each part right of its name ("Offred (red hair, ca. 30) ...... mezzo soprano"),
# and where tesseract left the leader out, the part is a line of its own beside the name: then the
# name is a row of a table, not a line of running text, though tesseract runs the names into one
# paragraph. A two-column page puts a whole line of text beside each line instead.
OCR_ROW_CELL_WORDS = 3


def is_row(line: Dict[str, Any], lines: List[Dict[str, Any]]) -> bool:
    """Whether a short line (a cell) stands right beside this one, overlapping it in height, or
    a dotted leader tesseract did not read as text runs on from it (ocr.fit_to_ink's
    `leader_after`: "Serena Joy (the Commander's wife, ca. 60) ........", its part in bold that
    tesseract did not read at all)."""
    if line.get("leader_after"):
        return True
    # Or one read stuck to its last word ("ca. 60) 1...", "(ca. 50).…", Ruders KA p. 7): unsure,
    # or more than an ellipsis, which a line of prose can end on too.
    if line.get("words"):
        last, conf = line["words"][-1][0], (line.get("confs") or [100.0])[-1]
        dots = TRAILING_DOTS.search(last)
        if dots and (conf < ocr.OCR_NOISE_MAX_CONFIDENCE or dots.group() not in ("...", "…")):
            return True
    left, top, right, bottom = line["box"]
    # Across a column gap: tesseract also breaks a line of prose in two at a wide space.
    return any(other["box"][0] - right > OCR_COLUMN_GAP * (bottom - top)
               and len(other["text"].split()) <= OCR_ROW_CELL_WORDS
               and min(bottom, other["box"][3]) - max(top, other["box"][1]) >= (bottom - top) / 2
               for other in lines if other is not line and not other["noise"])


def scan_paragraph_keys(lines: List[Dict[str, Any]], blocks: Sequence[Tuple[str, Any]],
                        rows: bool = False) -> List[Any]:
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
        if line.get("piece") or (not own and held(line, {"picture"})) or (rows and is_row(line, lines)):
            keys.append(("own", position))
            if rows:
                # It also ends the paragraph above it: the next line of the same tesseract
                # paragraph had otherwise joined the one before the row (Ruders KA p. 6, Moira's
                # line set after The Double's three rows up).
                part, current = part + 1, []
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
                      layout_model: bool = False, grouping: int = 0) -> List[Dict[str, Any]]:
    """A scanned document in extract_pdf_layout's shape, from the lines OCR found, so
    render_pdf_layout_overlay can set the translation where the text was printed.

    A score page goes line by line, its notation left out (see plan_pdf_page); any other page goes
    by tesseract's own paragraphs. Each paragraph also carries the boxes the original occupies,
    `cover` in the raw frame and `cover_px` in OCR pixels, for redact_translated_text to paint
    over: the text of a scan is pixels in an image, there is nothing to delete. With `source`, a
    score's lines in another language than the document's keep their original (secondary_lyrics).
    With `layout_model`, a prose page's paragraphs are split by heron's blocks
    (scan_paragraph_keys); a score page never is, heron finds hundreds of overlapping blocks there.
    `grouping` (SCAN_GROUPING) cuts a prose page's lines at column gaps first (split_columns) and
    takes a score's directions out of its sung lines (split_directions). Both are parameters so a
    history entry re-exports with the grouping it was translated with.
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
            if grouping < 3:
                lines = [{key: value for key, value in line.items() if key != "gap_ink"} for line in lines]
            if score and grouping >= 4:
                lines = [trimmed for line in lines for trimmed in trim_score_line(line)]
            if score and grouping >= 2:
                lines = [piece for line in lines for piece in
                         ([line] if line["noise"] else split_directions(line, numbers=grouping >= 3))]
            skip = secondary_lyrics(lines, source, proven) if score and source else set()
            directions = direction_groups(lines) if score else {}
            if score:
                keys: List[Any] = list(range(len(lines)))
            else:
                if grouping >= 1:
                    lines = [piece for line in lines for piece in split_columns(line, leaders=grouping >= 4)]
                scale_blocks = ocr.OCR_DPI / heron.HERON_DPI
                blocks = [(label, rect * scale_blocks) for label, rect in heron.page_blocks(page)] \
                    if layout_model and lines else []
                keys = scan_paragraph_keys(lines, blocks, rows=grouping >= 4)
            groups: Dict[Any, List[Dict[str, Any]]] = {}
            for position, line in enumerate(lines):
                if line["noise"] or position in skip or not pdf_layout.has_translatable_text(line["text"]):
                    continue
                groups.setdefault(keys[position], []).append(line)
            paragraphs = []
            for key, group in groups.items():
                rects = [pymupdf.Rect(line["box"]) * to_raw for line in group]
                if score:
                    text = join_sung_syllables(group[0]["text"])
                    whole = join_sung_syllables(group[0].get("untrimmed", group[0]["text"]))
                    if letter_share(whole) < OCR_SUNG_MIN_LETTERS or not OCR_SCORE_WORD.search(text)                             or OCR_SCORE_NAME.fullmatch(text.strip()):
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
                    # A direction is translated with the rest of its own lines, never with a sung one.
                    **({"direction": (number, directions[key])} if key in directions
                       else {"band": group[0]["par"][1]} if score and group[0].get("band") else {}),
                    "text": text,
                    "lines": [scan_line(line, rect, flip) for line, rect in zip(group, rects)],
                    # Reaching below the letters where an underline hangs under them (ocr.fit_to_ink).
                    "cover": [pymupdf.Rect(cover_box(line)) * to_raw
                              + (-OCR_COVER_PADDING, -OCR_COVER_PADDING, OCR_COVER_PADDING, OCR_COVER_PADDING)
                              for line in group],
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
                    if len(paragraph["lines"]) == 1 and grouping >= 4:
                        # A line on its own (a row of a list, a heading) stays one line: in a box of
                        # its own a longer translation shrinks instead of wrapping onto what lies
                        # below, which on a scan may be text OCR never read ("Mitte Zwanzig)" over
                        # the bold line under it, Ruders KA p. 7). Levelled with nothing, or one
                        # long row would set the page's body text smaller (see the score's lines).
                        line = paragraph["lines"][0]
                        paragraph["own_size"] = True
                        obstacles.append({"x": line["x"] - 1, "right": line["right"] + 1, "top": line["y"] + own,
                                          "bottom": line["y"] - OCR_GLYPH_DESCENT * own - 0.5})
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
                    own = size
                    if "direction" in paragraph:
                        # A direction or a footer set apart from the sung text in size keeps its
                        # own: the copyright line came out in lyric size over the ISBN beside it.
                        own = sorted(line["size"] for line in paragraph["lines"])[len(paragraph["lines"]) // 2]
                        own = size if abs(own - size) <= OCR_SIZE_SNAP * size else own
                    for line in paragraph["lines"]:
                        line["size"] = own
                        obstacles.append({"x": line["x"] - 1, "right": right, "top": line["y"] + own,
                                          "bottom": line["y"] - OCR_GLYPH_DESCENT * own - 0.5})
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
    direction ("Allegro", "Slowly") out. The lines of one stage direction (split_directions) are
    one group. Everything else stays a group of one.
    """
    groups: List[List[int]] = []
    open_by_voice: Dict[Tuple[int, int], List[int]] = {}
    directions: Dict[Any, List[int]] = {}
    texts: List[str] = []
    index = 0
    for page in pages:
        bands: Dict[int, List[Tuple[int, Dict[str, Any]]]] = {}
        for paragraph in page["paragraphs"]:
            texts.append(paragraph["text"])
            if "band" in paragraph:
                bands.setdefault(paragraph["band"], []).append((index, paragraph))
            elif "direction" in paragraph:
                group = directions.get(paragraph["direction"])
                if group and sum(len(texts[i]) for i in group) + len(paragraph["text"]) < config.MAX_CHARS:
                    group.append(index)
                else:
                    directions[paragraph["direction"]] = [index]
                    groups.append(directions[paragraph["direction"]])
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
                                     layout_model: bool = False, grouping: int = 0) -> bytes:
    """The history re-export of a scan overlay: read the pages again and zip the stored
    translations back on by position, as export_pdf_layout_with_translated_text does."""
    ocr_pages: Dict[int, List[Dict[str, Any]]] = {}
    pdf_text.extract_pdf_markdown_from_bytes(content, "application/pdf", page_range, source, ocr_pages)
    blocks = [block.strip() for block in translated_text.split("\n\n")]
    return pdf_layout.render_pdf_layout_overlay(
        content, scan_layout_pages(content, ocr_pages, page_range, language, layout_model, grouping), blocks)


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
