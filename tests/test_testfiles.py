"""Round-trip checks against the maintainer's real PDFs in tests/testfiles/.

Separate from test_main.py because that suite mocks the model away and runs anywhere, while
these need actual documents. tests/testfiles/ is gitignored, so every test here skips itself
when the directory is empty rather than failing on a fresh clone or in CI.

What "the same result comes out" means for the layout pipeline: translations are matched back to
paragraphs purely by position, and the history re-export re-extracts the original from scratch
(export_pdf_layout_with_translated_text). So extraction must be deterministic, must yield the
same paragraph count every time, and no paragraph may contain the blank line that the re-export
splits on, or every translation after it lands on the wrong paragraph.

Capped at MAX_PAGES per document: the long ones repeat the same layout constructs page after
page, so more pages cost runtime without covering anything new.
"""
import sys
import unittest
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.main as main
from fastapi import HTTPException

TESTFILES_DIR = Path(__file__).resolve().parent / "testfiles"
MAX_PAGES = 5


# Translated output the maintainer keeps for review lives here, next to the sources it was made
# from. It is a result, not an input, and must not be fed back in as one.
RESULTS_DIR = "testergebnisfiles"


def testfile_pdfs():
    # Recursive: the documents are sorted into subfolders ("Real Tests", "Language Tests/..."),
    # and a flat glob finds none of them, which turns the whole suite into a silent skip.
    if not TESTFILES_DIR.is_dir():
        return []
    return sorted(path for path in TESTFILES_DIR.rglob("*.pdf")
                  if RESULTS_DIR not in path.relative_to(TESTFILES_DIR).parts)


@lru_cache(maxsize=1)
def layout_pdfs():
    """The documents the layout pipeline can work on at all.

    Scanned and image-only PDFs are deliberately part of the set - they are what the OCR path is
    tested against - but they carry no positioned text, so extract_pdf_layout rejects them by
    design and every test below would fail on something that is working correctly.
    """
    usable = []
    for path in testfile_pdfs():
        try:
            main.extract_pdf_layout(path.read_bytes(), "1")
        except HTTPException:
            continue
        usable.append(path)
    return usable


def page_range_for(content: bytes) -> str:
    """"First MAX_PAGES pages", spelled so it survives documents with fewer pages than that.

    parse_page_range rejects "1-5" outright on a 1-page document instead of clamping, so the
    range has to be clamped by the caller (see TODO.md, Bugfixes).
    """
    total = len(main.PdfReader(BytesIO(content)).pages)
    return f"1-{min(MAX_PAGES, total)}"


@unittest.skipUnless(testfile_pdfs(), "tests/testfiles/ has no PDFs (gitignored, maintainer-local)")
class TestfilePdfRoundTripTests(unittest.TestCase):
    def test_layout_extraction_is_deterministic(self):
        # The history re-export re-extracts the original instead of storing the paragraph list,
        # so a second extraction that differs at all silently shifts every translation.
        for path in layout_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                page_range = page_range_for(content)
                first = main.extract_pdf_layout(content, page_range)
                second = main.extract_pdf_layout(content, page_range)

                self.assertEqual(
                    [[item["text"] for item in page["paragraphs"] + page["widgets"]] for page in first],
                    [[item["text"] for item in page["paragraphs"] + page["widgets"]] for page in second],
                )

    def test_no_paragraph_contains_the_blank_line_the_reexport_splits_on(self):
        # export_pdf_layout_with_translated_text splits the stored text on "\n\n" and zips the
        # blocks against re-extracted paragraphs by position. A paragraph holding a blank line
        # would produce more blocks than paragraphs and misalign everything after it.
        for path in layout_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                for page in main.extract_pdf_layout(content, page_range_for(content)):
                    for item in page["paragraphs"] + page["widgets"]:
                        self.assertNotIn("\n\n", item["text"])

    def test_identity_roundtrip_keeps_every_paragraph_aligned(self):
        # Feed each paragraph's own text back in as its "translation": the re-export must find
        # exactly as many paragraphs as there are blocks, so nothing shifts or gets dropped.
        for path in layout_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                page_range = page_range_for(content)
                pages = main.extract_pdf_layout(content, page_range)
                paragraphs = [item["text"] for page in pages
                              for item in page["paragraphs"] + page["widgets"]]
                self.assertTrue(paragraphs, f"{path.name} extracted no paragraphs at all")

                stored = "\n\n".join(paragraphs)
                blocks = stored.split("\n\n")
                self.assertEqual(len(blocks), len(paragraphs))

                exported = main.export_pdf_layout_with_translated_text(content, stored, page_range)
                self.assertTrue(exported.startswith(b"%PDF"))
                self.assertEqual(
                    len(main.PdfReader(BytesIO(exported)).pages),
                    len(pages),
                    "re-export must keep exactly the pages that were translated",
                )

    def test_selected_pages_only_are_exported(self):
        for path in layout_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                total = len(main.PdfReader(BytesIO(content)).pages)
                if total < 2:
                    self.skipTest("single-page document")
                pages = main.extract_pdf_layout(content, "2")
                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0]["number"], 2)


@unittest.skipUnless(testfile_pdfs(), "tests/testfiles/ has no PDFs (gitignored, maintainer-local)")
class TestfileRedactionTests(unittest.TestCase):
    def test_translated_pages_no_longer_carry_the_original_wording(self):
        """The original text must be gone from the file, not just painted over.

        Covering it left it copy/pasteable and findable with Ctrl+F, so a document handed on as
        "translated" still gave the reader the source wording. Checked by feeding each paragraph
        back in as its own "translation" and then asking the exported PDF for its text: only the
        substituted marker may survive.
        """
        for path in layout_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                pages = main.extract_pdf_layout(content, "1")
                paragraphs = [item["text"] for page in pages
                              for item in page["paragraphs"] + page["widgets"]]

                exported = main.export_pdf_layout_with_translated_text(
                    content, "\n\n".join("UEBERSETZT" for _ in paragraphs), "1"
                )
                left = pymupdf.open(stream=exported, filetype="pdf")[0].get_text()

                for original in paragraphs:
                    # Whole words only: a paragraph may share short fragments with a heading or
                    # a table cell that legitimately stayed put.
                    for word in original.split():
                        if len(word) > 8 and word.isalpha():
                            self.assertNotIn(word, left, f"{path.name}: {word!r} survived redaction")

    def test_redaction_keeps_the_page_backgrounds_and_rules(self):
        """Redaction must take link underlines with the text, and nothing else.

        An underline belongs to the words above it: left behind, it strikes through an unrelated
        part of the translation. But apply_redactions can just as easily strip the table shading,
        hint-box panels, rules and logos the text sits on, which is why it removes line art only
        where a drawing lies wholly inside one line's rectangle. Table rules span more than one
        cell and so survive; a short underline does not.
        """
        def solid_areas(page):
            return [d for d in page.get_drawings() if d["rect"].height >= 2.0 and d["rect"].width >= 2.0]

        for path in layout_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                pages = main.extract_pdf_layout(content, "1")
                paragraphs = [p for page in pages for p in page["paragraphs"]]
                before = pymupdf.open(stream=content, filetype="pdf")[0]

                redacted = main.redact_translated_text(
                    content, pages, ["UEBERSETZT" for _ in paragraphs]
                )
                after = pymupdf.open(stream=redacted, filetype="pdf")[0]

                # Not equality: rewriting the page can split a drawing or re-embed an image, so
                # the count may go up. Only losing one means the redaction ate the background.
                self.assertGreaterEqual(len(after.get_images()), len(before.get_images()))
                self.assertGreaterEqual(len(solid_areas(after)), len(solid_areas(before)))

    def test_redaction_removes_link_underlines_with_their_text(self):
        # Reddit_discussion is the document that has them: every link is underlined, and the
        # underlines used to stay put while the text under them changed length.
        document = next((p for p in layout_pdfs() if p.name.startswith("Reddit_discussion")), None)
        if document is None:
            self.skipTest("Reddit_discussion.pdf not in tests/testfiles/")

        content = document.read_bytes()
        pages = main.extract_pdf_layout(content, "1")
        paragraphs = [p for page in pages for p in page["paragraphs"]]

        def underlines(page):
            return [d for d in page.get_drawings() if d["rect"].height < 2.0 and d["rect"].width >= 2.0]

        before = pymupdf.open(stream=content, filetype="pdf")[0]
        self.assertTrue(underlines(before), "expected underlines in the source document")

        redacted = main.redact_translated_text(content, pages, ["UEBERSETZT" for _ in paragraphs])
        self.assertEqual(underlines(pymupdf.open(stream=redacted, filetype="pdf")[0]), [])


if __name__ == "__main__":
    unittest.main()
