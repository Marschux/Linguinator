import base64
import json
import os
import shutil
import time
import unittest
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main


class FakePage:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class FakeReader:
    def __init__(self, pages):
        self.pages = pages


class FakeInputs(dict):
    def to(self, device):
        self["device"] = device
        return self


class FakeTokenizer:
    def __init__(self):
        self.src_lang = None

    def __call__(self, text, return_tensors, truncation):
        return FakeInputs({"text": text, "return_tensors": return_tensors, "truncation": truncation})

    def convert_tokens_to_ids(self, target):
        return 42

    def batch_decode(self, generated, skip_special_tokens):
        return ["Uebersetzt"]


class FakeModel:
    def generate(self, **inputs):
        return ["generated"]


class FakeInferenceMode:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeTorch:
    def inference_mode(self):
        return FakeInferenceMode()


class FakeCuda:
    def __init__(self):
        self.emptied = False

    def is_available(self):
        return True

    def empty_cache(self):
        self.emptied = True


class FakeTorchWithCuda(FakeTorch):
    def __init__(self):
        self.cuda = FakeCuda()


class FakeCacheInfo:
    def __init__(self, currsize):
        self.currsize = currsize


class FakeCachedLoader:
    def __init__(self, value, currsize=1):
        self.value = value
        self.currsize = currsize
        self.cleared = False
        self.called = False

    def __call__(self):
        self.called = True
        return self.value

    def cache_info(self):
        return FakeCacheInfo(self.currsize)

    def cache_clear(self):
        self.cleared = True
        self.currsize = 0


def test_temp_dir():
    path = Path(__file__).resolve().parent / ".tmp-history" / str(uuid.uuid4())
    path.mkdir(parents=True, exist_ok=False)
    return path


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


def docx_with_extra_text_parts():
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>Body</w:t></w:r></w:p></w:body></w:document>'
    )
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:p><w:r><w:t>Header</w:t></w:r></w:p></w:hdr>'
    )
    footer = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:p><w:r><w:t>Footer</w:t></w:r></w:p></w:ftr>'
    )
    footnotes = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:footnote><w:p><w:r><w:t>Footnote</w:t></w:r></w:p></w:footnote></w:footnotes>'
    )
    comments = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:comment><w:p><w:r><w:t>Comment</w:t></w:r></w:p></w:comment></w:comments>'
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as docx:
        docx.writestr("word/document.xml", document)
        docx.writestr("word/header1.xml", header)
        docx.writestr("word/footer1.xml", footer)
        docx.writestr("word/footnotes.xml", footnotes)
        docx.writestr("word/comments.xml", comments)
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


def minimal_pptx(paragraphs):
    body = "".join(
        '<p:sp><p:txBody><a:p><a:r><a:t>' + text + '</a:t></a:r></a:p></p:txBody></p:sp>'
        for text in paragraphs
    )
    slide = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p:cSld><p:spTree>' + body + '</p:spTree></p:cSld></p:sld>'
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as pptx:
        pptx.writestr("ppt/slides/slide1.xml", slide)
    return buffer.getvalue()


def odt_with_span():
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        '<office:body><office:text><text:p><text:span text:style-name="Strong">First</text:span> Second</text:p>'
        '</office:text></office:body></office:document-content>'
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as odt:
        odt.writestr("content.xml", document)
    return buffer.getvalue()


def minimal_xlsx(target="worksheets/sheet1.xml", sheet_cell_type="inlineStr", sheet_value=None):
    if sheet_cell_type == "s":
        first_value = f'<c r="A1" t="s"><v>{sheet_value or "99"}</v></c>'
    else:
        first_value = '<c r="A1" t="inlineStr"><is><t>title</t></is></c>'

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
            f'<Relationship Id="rId1" Target="{target}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            "</Relationships>",
        )
        xlsx.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>title</t></si></sst>",
        )
        xlsx.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            f'<row r="1">{first_value}'
            '<c r="B1" t="inlineStr"><is><t>description</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>Hello</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>World</t></is></c></row>'
            "</sheetData></worksheet>",
        )
    return buffer.getvalue()


def xlsx_with_formula_cell():
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
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        xlsx.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>title</t></is></c>'
            '<c r="B1" t="inlineStr"><is><t>description</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>Hello</t></is></c>'
            '<c r="B2"><f>CONCAT(A2)</f><v>World</v></c></row>'
            "</sheetData></worksheet>",
        )
    return buffer.getvalue()


class MainTests(unittest.TestCase):
    def auth_header(self, username="admin", password="secret"):
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}

    def test_split_long_text_keeps_chunks_under_limit(self):
        text = "One two three four five six seven eight"

        chunks = main.split_long_text(text, 12)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 12 for chunk in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_basic_auth_allows_requests_when_disabled(self):
        with patch.object(main, "AUTH_ENABLED", False):
            response = TestClient(main.app).get("/")

        self.assertEqual(response.status_code, 200)

    def test_basic_auth_rejects_missing_credentials_when_enabled(self):
        with patch.object(main, "AUTH_ENABLED", True):
            with patch.object(main, "AUTH_PASSWORD", "secret"):
                response = TestClient(main.app).get("/")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], 'Basic realm="Lingumachina"')

    def test_basic_auth_rejects_wrong_credentials(self):
        with patch.object(main, "AUTH_ENABLED", True):
            with patch.object(main, "AUTH_USERNAME", "admin"):
                with patch.object(main, "AUTH_PASSWORD", "secret"):
                    response = TestClient(main.app).get("/", headers=self.auth_header(password="wrong"))

        self.assertEqual(response.status_code, 401)

    def test_basic_auth_accepts_configured_credentials(self):
        with patch.object(main, "AUTH_ENABLED", True):
            with patch.object(main, "AUTH_USERNAME", "admin"):
                with patch.object(main, "AUTH_PASSWORD", "secret"):
                    response = TestClient(main.app).get("/", headers=self.auth_header())

        self.assertEqual(response.status_code, 200)

    def test_health_stays_public_when_basic_auth_is_enabled(self):
        with patch.object(main, "AUTH_ENABLED", True):
            with patch.object(main, "AUTH_PASSWORD", "secret"):
                response = TestClient(main.app).get("/health")

        self.assertEqual(response.status_code, 200)

    def test_translate_one_uses_lazy_loaded_torch_module(self):
        with patch.object(main, "load_model", return_value=(FakeTokenizer(), FakeModel(), "cpu", FakeTorch())):
            translated = main.translate_one("Hello", "eng_Latn", "deu_Latn")

        self.assertEqual(translated, "Uebersetzt")

    def test_model_idle_unload_clears_cached_model_after_timeout(self):
        torch_module = FakeTorchWithCuda()
        model_loader = FakeCachedLoader((FakeTokenizer(), FakeModel(), "cuda", torch_module))
        tokenizer_loader = FakeCachedLoader(FakeTokenizer())

        with patch.object(main, "load_model", model_loader):
            with patch.object(main, "load_tokenizer", tokenizer_loader):
                with patch.object(main, "MODEL_IDLE_UNLOAD_ENABLED", True):
                    with patch.object(main, "MODEL_IDLE_SECONDS", 1200):
                        with patch.object(main, "MODEL_ACTIVE_USERS", 0):
                            with patch.object(main, "MODEL_LAST_USED", 100.0):
                                unloaded = main.unload_model_if_idle(now=1301.0)

        self.assertTrue(unloaded)
        self.assertTrue(model_loader.called)
        self.assertTrue(model_loader.cleared)
        self.assertTrue(tokenizer_loader.cleared)
        self.assertTrue(torch_module.cuda.emptied)

    def test_model_idle_unload_keeps_recent_model_loaded(self):
        model_loader = FakeCachedLoader((FakeTokenizer(), FakeModel(), "cpu", FakeTorch()))
        tokenizer_loader = FakeCachedLoader(FakeTokenizer())

        with patch.object(main, "load_model", model_loader):
            with patch.object(main, "load_tokenizer", tokenizer_loader):
                with patch.object(main, "MODEL_IDLE_UNLOAD_ENABLED", True):
                    with patch.object(main, "MODEL_IDLE_SECONDS", 1200):
                        with patch.object(main, "MODEL_ACTIVE_USERS", 0):
                            with patch.object(main, "MODEL_LAST_USED", 100.0):
                                unloaded = main.unload_model_if_idle(now=1000.0)

        self.assertFalse(unloaded)
        self.assertFalse(model_loader.cleared)
        self.assertFalse(tokenizer_loader.cleared)

    def test_model_idle_unload_skips_when_disabled_or_active(self):
        model_loader = FakeCachedLoader((FakeTokenizer(), FakeModel(), "cpu", FakeTorch()))
        tokenizer_loader = FakeCachedLoader(FakeTokenizer())

        with patch.object(main, "load_model", model_loader):
            with patch.object(main, "load_tokenizer", tokenizer_loader):
                with patch.object(main, "MODEL_IDLE_UNLOAD_ENABLED", False):
                    self.assertFalse(main.unload_model_if_idle(now=2000.0))
                with patch.object(main, "MODEL_IDLE_UNLOAD_ENABLED", True):
                    with patch.object(main, "MODEL_IDLE_SECONDS", 1200):
                        with patch.object(main, "MODEL_ACTIVE_USERS", 1):
                            with patch.object(main, "MODEL_LAST_USED", 100.0):
                                self.assertFalse(main.unload_model_if_idle(now=2000.0))

        self.assertFalse(model_loader.cleared)
        self.assertFalse(tokenizer_loader.cleared)

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

    def test_create_text_pdf_returns_pdf_document(self):
        content = main.create_text_pdf("Translated text\n\nSecond paragraph")

        self.assertTrue(content.startswith(b"%PDF-1.4"))
        self.assertIn(b"/Type /Page", content)
        self.assertIn(b"Translated text", content)
        self.assertIn(b"/Helvetica-Bold", content)

    def test_create_text_pdf_preserves_markdown_page_breaks(self):
        content = main.create_text_pdf("# Page 1\n\nFirst\n\n# Page 2\n\nSecond")

        self.assertIn(b"/Count 2", content)
        self.assertIn(b"Page 1", content)
        self.assertIn(b"Page 2", content)

    def test_create_text_pdf_formats_markdown_headings(self):
        content = main.create_text_pdf("# Title\n\nBody")

        self.assertIn(b"/F2 15 Tf", content)
        self.assertIn(b"Title", content)

    def test_create_pdf_from_pages_can_add_cover_rectangle(self):
        content = main.create_pdf_from_pages(
            [{
                "source_page": "1",
                "continuation": False,
                "lines": [{"text": "Translated", "font": "F1", "size": 11, "line_height": 14}],
            }],
            cover_original=True,
        )

        self.assertIn(b" re f", content)
        self.assertIn(b"Translated", content)

    def test_create_overlay_pdf_preserves_original_page_count(self):
        original = main.create_text_pdf("# Page 1\n\nOriginal\n\n# Page 2\n\nSecond")

        overlay = main.create_overlay_pdf(original, "# Page 1\n\nTranslated\n\n# Page 2\n\nSecond translated")

        reader = main.PdfReader(BytesIO(overlay))
        self.assertEqual(len(reader.pages), 2)

    def test_pdf_text_object_encodes_unicode(self):
        self.assertEqual(main.pdf_text_object("Grusse"), "(Grusse)")
        self.assertEqual(main.pdf_text_object("Gruesse aeoeue"), "(Gruesse aeoeue)")
        self.assertTrue(main.pdf_text_object("Gruesse: " + chr(228)).startswith("<FEFF"))

    def test_cleanup_history_removes_old_files(self):
        temp_dir = test_temp_dir()
        try:
            old_file = temp_dir / "old.md"
            fresh_file = temp_dir / "fresh.md"
            old_file.write_text("old", encoding="utf-8")
            fresh_file.write_text("fresh", encoding="utf-8")
            old_time = time.time() - (main.HISTORY_DAYS * 86400) - 60
            os.utime(old_file, (old_time, old_time))

            with patch.object(main, "HISTORY_DIR", temp_dir):
                main.cleanup_history()

            self.assertFalse(old_file.exists())
            self.assertTrue(fresh_file.exists())
        finally:
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_history_items_include_file_size(self):
        temp_dir = test_temp_dir()
        try:
            item_id = "2026-08-02-text-abc123"
            md_path = temp_dir / f"{item_id}.md"
            json_path = temp_dir / f"{item_id}.json"
            md_path.write_text("result", encoding="utf-8")
            json_path.write_text(json.dumps({
                "id": item_id,
                "kind": "text",
                "source": "eng_Latn",
                "target": "deu_Latn",
                "created_at": "2026-08-02T00:00:00+00:00",
                "filename": md_path.name,
            }), encoding="utf-8")

            with patch.object(main, "HISTORY_DIR", temp_dir):
                items = main.history_items()

            self.assertEqual(items[0]["size_bytes"], len("result"))
        finally:
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_docx_extraction_preserves_paragraphs(self):
        content = minimal_docx(["First paragraph", "Second paragraph"])

        text = main.extract_docx_text_from_bytes(content)

        self.assertEqual(text, "First paragraph\n\nSecond paragraph")

    def test_docx_export_replaces_paragraph_text(self):
        content = minimal_docx(["First paragraph", "Second paragraph"])

        updated = main.export_docx_with_translated_text(content, "Erster Absatz\n\nZweiter Absatz")

        self.assertEqual(main.extract_docx_text_from_bytes(updated), "Erster Absatz\n\nZweiter Absatz")

    def test_docx_export_replaces_headers_footnotes_and_comments(self):
        content = docx_with_extra_text_parts()

        updated = main.export_docx_with_translated_text(
            content,
            "Body neu\n\nHeader neu\n\nFooter neu\n\nFootnote neu\n\nComment neu",
        )

        self.assertEqual(
            main.extract_docx_text_from_bytes(updated),
            "Body neu\n\nHeader neu\n\nFooter neu\n\nFootnote neu\n\nComment neu",
        )

    def test_zip_size_limit_rejects_large_uncompressed_archives(self):
        content = minimal_docx(["First paragraph"])

        with patch.object(main, "MAX_ZIP_UNCOMPRESSED_BYTES", 1):
            with self.assertRaises(HTTPException) as raised:
                main.extract_docx_text_from_bytes(content)

        self.assertEqual(raised.exception.status_code, 413)

    def test_zip_rewrite_rejects_unsafe_archive_paths(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("../bad.xml", "bad")

        with self.assertRaises(HTTPException) as raised:
            main.write_zip_with_replacement(buffer.getvalue(), {})

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("unsafe paths", raised.exception.detail)

    def test_odt_extraction_preserves_paragraphs(self):
        content = minimal_odt(["First paragraph", "Second paragraph"])

        text = main.extract_odt_text_from_bytes(content)

        self.assertEqual(text, "First paragraph\n\nSecond paragraph")

    def test_odt_export_replaces_paragraph_text(self):
        content = minimal_odt(["First paragraph", "Second paragraph"])

        updated = main.export_odt_with_translated_text(content, "Erster Absatz\n\nZweiter Absatz")

        self.assertEqual(main.extract_odt_text_from_bytes(updated), "Erster Absatz\n\nZweiter Absatz")

    def test_odt_export_preserves_inline_span_structure(self):
        content = odt_with_span()

        updated = main.export_odt_with_translated_text(content, "Erster Absatz")

        self.assertEqual(main.extract_odt_text_from_bytes(updated), "Erster Absatz")
        with zipfile.ZipFile(BytesIO(updated)) as odt:
            content_xml = odt.read("content.xml")
        self.assertIn(b"text:span", content_xml)
        self.assertIn(b'text:style-name="Strong"', content_xml)

    def test_pptx_extraction_and_export_replace_slide_text(self):
        content = minimal_pptx(["Title", "Subtitle"])

        text = main.extract_pptx_text_from_bytes(content)
        updated = main.export_pptx_with_translated_text(content, "Titel\n\nUntertitel")

        self.assertEqual(text, "Title\n\nSubtitle")
        self.assertEqual(main.extract_pptx_text_from_bytes(updated), "Titel\n\nUntertitel")

    def test_csv_extraction_uses_selected_columns(self):
        content = b"title,description,ignore\nHello,World,Nope\nSecond,Row,Skip\n"

        text = main.extract_csv_text_from_bytes(content, "title, description")

        self.assertEqual(text, "Hello | World\n\nSecond | Row")

    def test_csv_export_replaces_selected_columns(self):
        content = b"title,description,ignore\nHello,World,Nope\nSecond,Row,Skip\n"

        updated = main.export_csv_with_translated_text(content, "title, description", "Hallo | Welt\n\nZweite | Zeile")

        self.assertIn(b"Hallo,Welt,Nope", updated)
        self.assertIn(b"Zweite,Zeile,Skip", updated)

    def test_csv_extraction_reports_missing_columns(self):
        content = b"title,description\nHello,World\n"

        with self.assertRaises(HTTPException) as raised:
            main.extract_csv_text_from_bytes(content, "missing")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("CSV columns not found", raised.exception.detail)

    def test_xlsx_extraction_uses_selected_columns(self):
        text = main.extract_xlsx_text_from_bytes(minimal_xlsx(), "Sheet1", "title,description")

        self.assertEqual(text, "Hello | World")

    def test_xlsx_export_replaces_selected_columns(self):
        updated = main.export_xlsx_with_translated_text(minimal_xlsx(), "Sheet1", "title,description", "Hallo | Welt")

        text = main.extract_xlsx_text_from_bytes(updated, "Sheet1", "title,description")
        self.assertEqual(text, "Hallo | Welt")

    def test_xlsx_export_preserves_formula_and_updates_cached_value(self):
        updated = main.export_xlsx_with_translated_text(xlsx_with_formula_cell(), "Sheet1", "description", "Welt")

        text = main.extract_xlsx_text_from_bytes(updated, "Sheet1", "description")
        self.assertEqual(text, "Welt")
        with zipfile.ZipFile(BytesIO(updated)) as xlsx:
            sheet = xlsx.read("xl/worksheets/sheet1.xml")
        self.assertIn(b"<s:f>CONCAT(A2)</s:f>", sheet)
        self.assertIn(b"<s:v>Welt</s:v>", sheet)

    def test_xlsx_extraction_resolves_absolute_sheet_target(self):
        text = main.extract_xlsx_text_from_bytes(minimal_xlsx("/xl/worksheets/sheet1.xml"), "Sheet1", "title")

        self.assertEqual(text, "Hello")

    def test_xlsx_extraction_reports_bad_shared_string_reference(self):
        with self.assertRaises(HTTPException) as raised:
            main.extract_xlsx_text_from_bytes(minimal_xlsx(sheet_cell_type="s", sheet_value="99"), "Sheet1", "title")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Invalid XLSX shared string reference", raised.exception.detail)

    def test_html_extraction_and_export_preserve_markup(self):
        content = b"<html><body><h1>Hello</h1><script>ignore()</script><p>World</p></body></html>"

        text = main.extract_html_text_from_bytes(content)
        updated = main.export_html_with_translated_text(content, "Hallo\n\nWelt")

        self.assertEqual(text, "Hello\n\nWorld")
        self.assertIn(b"<h1>Hallo</h1>", updated)
        self.assertIn(b"<script>ignore()</script>", updated)

    def test_subtitle_extraction_and_export_keep_timings(self):
        content = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\nWorld\n"

        text = main.extract_subtitle_text_from_bytes(content)
        updated = main.export_subtitle_with_translated_text(content, "Hallo\n\nWelt")

        self.assertEqual(text, "Hello\n\nWorld")
        self.assertIn(b"00:00:01,000 --> 00:00:02,000\nHallo", updated)
        self.assertIn(b"00:00:03,000 --> 00:00:04,000\nWelt", updated)

    def test_json_extraction_and_export_replace_string_values(self):
        content = b'{"title":"Hello","items":["World", 3]}'

        text = main.extract_json_text_from_bytes(content)
        updated = main.export_json_with_translated_text(content, "Hallo\n\nWelt")

        self.assertEqual(text, "Hello\n\nWorld")
        self.assertEqual(json.loads(updated.decode("utf-8")), {"title": "Hallo", "items": ["Welt", 3]})

    def test_yaml_extraction_and_export_replace_simple_scalars(self):
        content = b"title: Hello\ncount: 3\nnested:\n  text: World\n"

        text = main.extract_yaml_text_from_bytes(content)
        updated = main.export_yaml_with_translated_text(content, "Hallo\n\nWelt")

        self.assertEqual(text, "Hello\n\nWorld")
        self.assertIn(b"title: Hallo", updated)
        self.assertIn(b"  text: Welt", updated)

    def test_po_extraction_and_export_update_msgstr(self):
        content = b'msgid "Hello"\nmsgstr ""\n\nmsgid "World"\nmsgstr "Existing"\n'

        text = main.extract_po_text_from_bytes(content)
        updated = main.export_po_with_translated_text(content, "Hallo\n\nWelt")

        self.assertEqual(text, "Hello\n\nExisting")
        self.assertIn('msgstr "Hallo"'.encode("utf-8"), updated)
        self.assertIn('msgstr "Welt"'.encode("utf-8"), updated)

    def test_xliff_extraction_and_export_update_targets(self):
        content = (
            b'<xliff version="1.2"><file><body>'
            b'<trans-unit id="1"><source>Hello</source><target></target></trans-unit>'
            b'<trans-unit id="2"><source>World</source></trans-unit>'
            b'</body></file></xliff>'
        )

        text = main.extract_xliff_text_from_bytes(content)
        updated = main.export_xliff_with_translated_text(content, "Hallo\n\nWelt")

        self.assertEqual(text, "Hello\n\nWorld")
        self.assertIn(b"<target>Hallo</target>", updated)
        self.assertIn(b"<target>Welt</target>", updated)

    def test_xliff_export_creates_namespaced_targets(self):
        content = (
            b'<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2">'
            b'<file><body><trans-unit id="1"><source>Hello</source></trans-unit></body></file></xliff>'
        )

        updated = main.export_xliff_with_translated_text(content, "Hallo")

        self.assertIn(b"<xlf:target>Hallo</xlf:target>", updated)


if __name__ == "__main__":
    unittest.main()
