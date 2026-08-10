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
import zlib
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.main as main

TESTFILES_DIR = Path(__file__).resolve().parent / "testfiles"
MAX_PAGES = 5


def testfile_pdfs():
    if not TESTFILES_DIR.is_dir():
        return []
    return sorted(TESTFILES_DIR.glob("*.pdf"))


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
        for path in testfile_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                page_range = page_range_for(content)
                first = main.extract_pdf_layout(content, page_range)
                second = main.extract_pdf_layout(content, page_range)

                self.assertEqual(
                    [[p["text"] for p in page["paragraphs"]] for page in first],
                    [[p["text"] for p in page["paragraphs"]] for page in second],
                )

    def test_no_paragraph_contains_the_blank_line_the_reexport_splits_on(self):
        # export_pdf_layout_with_translated_text splits the stored text on "\n\n" and zips the
        # blocks against re-extracted paragraphs by position. A paragraph holding a blank line
        # would produce more blocks than paragraphs and misalign everything after it.
        for path in testfile_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                for page in main.extract_pdf_layout(content, page_range_for(content)):
                    for paragraph in page["paragraphs"]:
                        self.assertNotIn("\n\n", paragraph["text"])

    def test_identity_roundtrip_keeps_every_paragraph_aligned(self):
        # Feed each paragraph's own text back in as its "translation": the re-export must find
        # exactly as many paragraphs as there are blocks, so nothing shifts or gets dropped.
        for path in testfile_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                page_range = page_range_for(content)
                pages = main.extract_pdf_layout(content, page_range)
                paragraphs = [p["text"] for page in pages for p in page["paragraphs"]]
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
        for path in testfile_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                total = len(main.PdfReader(BytesIO(content)).pages)
                if total < 2:
                    self.skipTest("single-page document")
                pages = main.extract_pdf_layout(content, "2")
                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0]["number"], 2)


@unittest.skipUnless(testfile_pdfs(), "tests/testfiles/ has no PDFs (gitignored, maintainer-local)")
class TestfileCoverBoxTests(unittest.TestCase):
    def cover_cells_with_regions(self, image, page, paragraph):
        """Each cover cell the pipeline would emit for this paragraph, paired with the slice of
        the rendered page it was sampled from. Mirrors the cover geometry in reflow_paragraph and
        the cell grid in pdf_page_pixel_cropper, so a cell can be checked against its own source.
        """
        page_width, page_height = page["width"], page["height"]
        crop = main.pdf_page_pixel_cropper(image, page_width, page_height)
        self.assertIsNotNone(crop, "Pillow unavailable, cannot sample covers")
        scale_x = image.width / page_width
        scale_y = image.height / page_height

        for line in paragraph["lines"]:
            x = line["x"] - 1
            y = line["y"] - 0.25 * line["size"]
            width = (line["right"] - line["x"]) + 2
            height = 1.2 * line["size"]
            sample = crop(x, y, width, height)
            if not sample:
                continue
            data, cells_across, cells_down = sample
            raw = zlib.decompress(data)

            left = max(0, min(image.width - 1, int(x * scale_x)))
            right = max(left + 1, min(image.width, int((x + width) * scale_x)))
            top = max(0, min(image.height - 1, int((page_height - (y + height)) * scale_y)))
            bottom = max(top + 1, min(image.height, int((page_height - y) * scale_y)))
            cell_width = (right - left) / cells_across
            cell_height = (bottom - top) / cells_down

            for row in range(cells_down):
                for column in range(cells_across):
                    offset = (row * cells_across + column) * 3
                    cell_left = left + int(column * cell_width)
                    cell_top = top + int(row * cell_height)
                    region = image.crop((
                        cell_left,
                        cell_top,
                        max(cell_left + 1, left + int((column + 1) * cell_width)),
                        max(cell_top + 1, top + int((row + 1) * cell_height)),
                    ))
                    yield tuple(raw[offset:offset + 3]), region

    def test_cover_fills_are_colours_that_exist_on_the_page(self):
        """Every cover fill must be a colour actually present in the area it covers.

        This is the precise form of the "grey bands over every line" regression: averaging the
        region blends the glyphs being hidden into the fill and yields a colour that appears
        nowhere on the page (black text on white averaged to ~(205,205,205)). Sampling the
        region's dominant colour instead can only ever return a colour that is really there.

        Deliberately not asserting "covers are light": these documents carry black table headers,
        grey panels and hint boxes, and a cover sitting on one of those is correct to be dark.
        """
        for path in testfile_pdfs():
            with self.subTest(document=path.name):
                content = path.read_bytes()
                pages = main.extract_pdf_layout(content, "1")
                image = main.render_pdf_page_image(content, 1)
                if image is None:
                    self.skipTest("pdftoppm unavailable")

                page = pages[0]
                checked = 0
                invented = []
                for paragraph in page["paragraphs"]:
                    for cell, region in self.cover_cells_with_regions(image, page, paragraph):
                        checked += 1
                        present = {color for _count, color in region.getcolors(
                            maxcolors=region.width * region.height + 1
                        ) or []}
                        if cell not in present:
                            invented.append((cell, sorted(present)[:3]))

                self.assertTrue(checked, f"{path.name} produced no cover samples")
                self.assertEqual(
                    invented[:5],
                    [],
                    f"{path.name}: {len(invented)}/{checked} cover fills are blended colours that "
                    f"do not occur in the region they cover",
                )


if __name__ == "__main__":
    unittest.main()
