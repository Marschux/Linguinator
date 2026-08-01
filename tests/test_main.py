import json
import os
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import app.main as main


class FakePage:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class FakeReader:
    def __init__(self, pages):
        self.pages = pages


def minimal_docx(paragraphs):
    body = "".join(
        "<w:p><w:r><w:t>" + text + "</w:t></w:r></w:p>"
        for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + body + "</w:body></w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as docx:
        docx.writestr("word/document.xml", document)
    return buffer.getvalue()


def minimal_odt(paragraphs):
    body = "".join(
        "<text:p>" + text + "</text:p>"
        for text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:text>" + body + "</office:text></office:body>"
        "</office:document-content>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as odt:
        odt.writestr("content.xml", document)
    return buffer.getvalue()


def minimal_xlsx():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as xlsx:
        xlsx.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        xlsx.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            "</Relationships>",
        )
        xlsx.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>title</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>description</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>Hello</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>World</t></is></c></row>'
            "</sheetData></worksheet>",
        )
    return buffer.getvalue()


class MainTests(unittest.TestCase):
    def test_split_long_text_keeps_chunks_under_limit(self):
        text = "One two three four five six seven eight"

        chunks = main.split_long_text(text, 12)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 12 for chunk in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_pdf_extraction_marks_empty_pages(self):
        reader = FakeReader([FakePage("Hello PDF\n"), FakePage("")])

        with patch.object(main, "PdfReader", return_value=reader):
            markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("# Page 1", markdown)
        self.assertIn("Hello PDF", markdown)
        self.assertIn("# Page 2", markdown)
        self.assertIn("No extractable text found", markdown)

    def test_pdf_extraction_marks_low_text_pages(self):
        reader = FakeReader([FakePage("tiny")])

        with patch.object(main, "PdfReader", return_value=reader):
            markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("very little extractable text", markdown)

    def test_pdf_extraction_uses_page_range(self):
        reader = FakeReader([FakePage("Page one long text"), FakePage("Page two long text")])

        with patch.object(main, "PdfReader", return_value=reader):
            markdown = main.extract_pdf_markdown_from_bytes(b"%PDF", page_range="2")

        self.assertNotIn("# Page 1", markdown)
        self.assertIn("# Page 2", markdown)

    def test_pdf_extraction_reports_scanned_pdf(self):
        reader = FakeReader([FakePage(""), FakePage(None)])

        with patch.object(main, "PdfReader", return_value=reader):
            with self.assertRaises(HTTPException) as raised:
                main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("OCR is disabled", raised.exception.detail)

    def test_pdf_extraction_uses_ocr_for_empty_pages_when_enabled(self):
        reader = FakeReader([FakePage("")])

        with patch.object(main, "PdfReader", return_value=reader):
            with patch.object(main, "OCR_ENABLED", True):
                with patch.object(main, "ocr_pdf_page", return_value="OCR text"):
                    markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("OCR text", markdown)

    def test_cleanup_history_removes_old_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_file = Path(temp_dir) / "old.md"
            fresh_file = Path(temp_dir) / "fresh.md"
            old_file.write_text("old", encoding="utf-8")
            fresh_file.write_text("fresh", encoding="utf-8")
            old_time = time.time() - (main.HISTORY_DAYS * 86400) - 60
            os.utime(old_file, (old_time, old_time))

            with patch.object(main, "HISTORY_DIR", Path(temp_dir)):
                main.cleanup_history()

            self.assertFalse(old_file.exists())
            self.assertTrue(fresh_file.exists())

    def test_history_items_include_file_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            item_id = "2026-08-02-text-abc123"
            md_path = Path(temp_dir) / f"{item_id}.md"
            json_path = Path(temp_dir) / f"{item_id}.json"
            md_path.write_text("result", encoding="utf-8")
            json_path.write_text(json.dumps({
                "id": item_id,
                "kind": "text",
                "source": "eng_Latn",
                "target": "deu_Latn",
                "created_at": "2026-08-02T00:00:00+00:00",
                "filename": md_path.name,
            }), encoding="utf-8")

            with patch.object(main, "HISTORY_DIR", Path(temp_dir)):
                items = main.history_items()

        self.assertEqual(items[0]["size_bytes"], len("result"))

    def test_docx_extraction_preserves_paragraphs(self):
        content = minimal_docx(["First paragraph", "Second paragraph"])

        text = main.extract_docx_text_from_bytes(content)

        self.assertEqual(text, "First paragraph\n\nSecond paragraph")

    def test_odt_extraction_preserves_paragraphs(self):
        content = minimal_odt(["First paragraph", "Second paragraph"])

        text = main.extract_odt_text_from_bytes(content)

        self.assertEqual(text, "First paragraph\n\nSecond paragraph")

    def test_csv_extraction_uses_selected_columns(self):
        content = b"title,description,ignore\nHello,World,Nope\nSecond,Row,Skip\n"

        text = main.extract_csv_text_from_bytes(content, "title, description")

        self.assertEqual(text, "Hello | World\n\nSecond | Row")

    def test_csv_extraction_reports_missing_columns(self):
        content = b"title,description\nHello,World\n"

        with self.assertRaises(HTTPException) as raised:
            main.extract_csv_text_from_bytes(content, "missing")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("CSV columns not found", raised.exception.detail)

    def test_xlsx_extraction_uses_selected_columns(self):
        text = main.extract_xlsx_text_from_bytes(minimal_xlsx(), "Sheet1", "title,description")

        self.assertEqual(text, "Hello | World")


if __name__ == "__main__":
    unittest.main()
