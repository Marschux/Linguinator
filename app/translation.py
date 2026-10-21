"""Sentence splitting, the hallucination guard, model calls and chunked translation."""
import re
from typing import Dict, List, Set, Tuple
from . import config, langs, models, jobqueue, pdf_layout


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


def model_input(model_id: str, text: str, target: str) -> str:
    """The text as this model expects it: the multilingual one needs the target's >>xxx<< in front."""
    if langs.model_family(model_id) == "prefix":
        return f"{langs.model_language_code(model_id, target)} {text}"
    return text


def run_translator(model_id: str, texts: List[str]) -> List[str]:
    """One translator call for several sentences, each its own entry in the batch."""
    tokenizer, translator = models.load_model(model_id)
    # The explicit max_length matters: opus-mt-en-de/de-en leave model_max_length unset, so
    # truncation=True alone is a no-op and a long piece overruns the model's positions.
    batch = [tokenizer.convert_ids_to_tokens(
        tokenizer(text, truncation=True, max_length=TRANSLATE_MAX_TOKENS).input_ids) for text in texts]
    results = translator.translate_batch(
        batch, beam_size=4, max_decoding_length=TRANSLATE_MAX_TOKENS, no_repeat_ngram_size=NO_REPEAT_NGRAM_SIZE,
    )
    return [tokenizer.decode(tokenizer.convert_tokens_to_ids(result.hypotheses[0]), skip_special_tokens=True)
            for result in results]


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

# The same ligatures from producers whose ToUnicode table points them at Latin Extended-B
# (Stall-Kamera-System: "PosiƟon", "FestplaƩe", "SoŌware"). Replaced only after a lowercase
# letter, since each is a real capital elsewhere: Ō carries the macron of Latin and of romanised
# Japanese.
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

# The factor counts characters, and Han and kana carry a whole word in one or two, so a faithful
# German translation of Japanese is several times its source. Weighting such a character as 2.5
# passes every genuine ja/zh -> de translation measured (x1.70 to x6.25, Aug 2026) and still
# catches the training-data dumps, which run 100+ characters off a heading of ten.
#
# Hangul is deliberately unweighted: the model answers Korean with Bible boilerplate (x3.90, one
# loop at x34.30), and the unweighted budget is what keeps that off the page.
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


# English curly double quotes make OPUS-MT stop mid-sentence: MatterhornProtokoll's "... von
# “Software” zu “Mensch”, aktualisierte Beschreibung auf Seite 2, ..." came back as "Change the test
# method at 2 test points from" and nothing else, in transformers and CTranslate2 alike. The same
# sentence with German „ “, straight quotes or none came back whole, and the model answers in
# straight quotes anyway. Apostrophes (’) are left alone; they caused no such loss.
CURLY_DOUBLE_QUOTES = str.maketrans({"“": '"', "”": '"'})


# Symbol and Wingdings glyphs come out of a PDF as Private Use characters (U+E000-U+F8FF), U+F0B7
# being Word's bullet. The model has never seen one and puts something of its own in its place:
# MatterhornProtokoll's "... erfüllt:  Die Schrift verwendet MacRomanEncoding" came back with a
# "的" there, and guard_hallucination rightly threw the whole sentence away.
# ponytail: every such glyph becomes a bullet; map more of them if icons (a phone, a mail) matter.
PRIVATE_USE = re.compile("[-]")


def clean_source_text(text: str) -> str:
    text = PRIVATE_USE.sub("•", LEADER_RUN.sub(" ", text))
    return text.translate(CURLY_DOUBLE_QUOTES).strip()


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
    return translate_batch([text], source, target)[0]


def translate_batch(texts: List[str], source: str, target: str) -> List[str]:
    """Translate several texts in one translator call. Every sentence is its own entry in the
    batch, never several in one (see split_to_sentences), and each result maps back to its text
    by position."""
    texts = [clean_source_text(text) for text in texts]
    indices = [i for i, text in enumerate(texts) if text]
    if not indices:
        return ["" for _ in texts]

    model_id, _dedicated = langs.resolve_model(source, target)
    models.begin_model_use()
    try:
        tokenizer, _translator = models.load_model(model_id)
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
        decoded = run_translator(model_id, [model_input(model_id, text, target) for text in batch_texts])
    finally:
        models.end_model_use()

    parts: Dict[int, List[str]] = {}
    for owner, source_text, text in zip(owners, sources, decoded):
        # Guarded per sentence, because that is the unit the model invents in: one made-up
        # sentence in the middle of a paragraph used to take the whole paragraph down with it.
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
    chunks = split_long_text(text, config.MAX_CHARS)
    translated = [translate_one(chunk, source, target) for chunk in chunks]
    return "\n\n".join(translated)


def translate_chunks(chunks: List[str], source: str, target: str, job_id: str, mixed: bool = False) -> List[str]:
    translated = list(chunks)
    done = 0
    for language, positions in langs.language_groups(chunks, source, target, mixed):
        for index in positions:
            jobqueue.wait_if_paused_or_cancelled(job_id)
            jobqueue.update_job(job_id, status="running", current=done, message=f"Translating chunk {done + 1}")
            translated[index] = translate_one(chunks[index], language, target)
            done += 1
            jobqueue.update_job(job_id, current=done)
    return translated


def translate_chunks_batched(chunks: List[str], source: str, target: str, job_id: str, batch_size: int,
                             mixed: bool = False) -> List[str]:
    translated = list(chunks)
    done = 0
    for language, positions in langs.language_groups(chunks, source, target, mixed):
        for start in range(0, len(positions), batch_size):
            jobqueue.wait_if_paused_or_cancelled(job_id)
            group = positions[start:start + batch_size]
            jobqueue.update_job(job_id, status="running", current=done, message=f"Translating {done} / {len(chunks)} chunks")
            for index, text in zip(group, translate_batch([chunks[i] for i in group], language, target)):
                translated[index] = text
            done += len(group)
            jobqueue.update_job(job_id, current=done)
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


def translate_markdown_document(markdown: str, source: str, target: str, job_id: str,
                                mixed: bool = False) -> Tuple[str, int]:
    blocks = split_markdown_blocks(markdown)
    # Only lines with something to translate are sent; the markers, blank lines and any
    # wordless leftovers (fenced code included) keep their place so the document reassembles
    # line for line.
    translatable = [index for index, (_prefix, text) in enumerate(blocks) if pdf_layout.has_translatable_text(text)]
    chunks = [blocks[index][1] for index in translatable]
    jobqueue.update_job(job_id, total=len(chunks), message=f"Translating 0 / {len(chunks)} chunks")
    translated_chunks = translate_chunks_batched(chunks, source, target, job_id, config.PDF_LAYOUT_BATCH_SIZE, mixed)

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
