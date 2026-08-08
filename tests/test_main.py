import base64
import json
import os
import shutil
import sys
import time
import unittest
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
import app.server as server


def pdf_text(content):
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)


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

    def __call__(self, text, return_tensors, truncation, max_length=None):
        return FakeInputs({"text": text, "return_tensors": return_tensors, "truncation": truncation, "max_length": max_length})

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


class FakeTorchWithThreads(FakeTorch):
    def __init__(self):
        self.threads = None
        self.interop_threads = None

    def set_num_threads(self, threads):
        self.threads = threads

    def set_num_interop_threads(self, threads):
        self.interop_threads = threads


class FakeCacheInfo:
    def __init__(self, currsize):
        self.currsize = currsize


class FakeCachedLoader:
    def __init__(self, value, currsize=1):
        self.value = value
        self.currsize = currsize
        self.cleared = False
        self.called = False

    def __call__(self, *args):
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
        self.assertEqual(response.headers["www-authenticate"], 'Basic realm="Linguinator"')

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

    def test_health_reports_app_version(self):
        response = TestClient(main.app).get("/health")

        self.assertEqual(response.json()["version"], main.app.version)

    def test_health_reports_proxy_configuration(self):
        with patch.object(main, "ROOT_PATH", "/linguinator"):
            with patch.object(main, "PUBLIC_URL", "https://example.test/linguinator"):
                with patch.object(main, "TRUST_PROXY_HEADERS", True):
                    response = TestClient(main.app).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["root_path"], "/linguinator")
        self.assertEqual(response.json()["public_url"], "https://example.test/linguinator")
        self.assertTrue(response.json()["trust_proxy_headers"])

    def test_root_path_is_normalized_for_reverse_proxy_prefixes(self):
        self.assertEqual(main.normalized_root_path("linguinator"), "/linguinator")
        self.assertEqual(main.normalized_root_path("/linguinator/"), "/linguinator")
        self.assertEqual(main.normalized_root_path(""), "")

    def test_env_value_prefers_set_variable_over_default(self):
        with patch.dict(os.environ, {"LINGUINATOR_MODEL": "new"}, clear=False):
            self.assertEqual(main.env_value("LINGUINATOR_MODEL", "default"), "new")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main.env_value("LINGUINATOR_MODEL", "default"), "default")

    def test_server_reads_proxy_and_https_env(self):
        env = {
            "LINGUINATOR_HOST": "127.0.0.1",
            "LINGUINATOR_PORT": "5443",
            "LINGUINATOR_TRUST_PROXY_HEADERS": "true",
            "LINGUINATOR_FORWARDED_ALLOW_IPS": "10.0.0.1",
            "LINGUINATOR_SSL_CERTFILE": "/certs/fullchain.pem",
            "LINGUINATOR_SSL_KEYFILE": "/certs/privkey.pem",
        }

        with patch.dict(os.environ, env, clear=False):
            with patch.object(server.uvicorn, "run") as run:
                server.main()

        run.assert_called_once()
        options = run.call_args.kwargs
        self.assertEqual(options["app"], "app.main:app")
        self.assertEqual(options["host"], "127.0.0.1")
        self.assertEqual(options["port"], 5443)
        self.assertTrue(options["proxy_headers"])
        self.assertEqual(options["forwarded_allow_ips"], "10.0.0.1")
        self.assertEqual(options["ssl_certfile"], "/certs/fullchain.pem")
        self.assertEqual(options["ssl_keyfile"], "/certs/privkey.pem")

    def test_frontend_uses_relative_paths_for_reverse_proxy_prefixes(self):
        template = (Path(main.APP_DIR) / "templates" / "index.html").read_text(encoding="utf-8")
        script = (Path(main.APP_DIR) / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('href="static/styles.css"', template)
        self.assertIn('src="static/app.js"', template)
        self.assertIn('id="uiLanguage"', template)
        self.assertIn('id="inputTabSelect"', template)
        self.assertIn('id="clearInput"', template)
        self.assertIn('value="de">Deutsch', template)
        self.assertNotIn("previewToggle", template)
        self.assertNotIn("historyToggle", template)
        self.assertNotIn('fetch("/', script)
        self.assertNotIn('href = "/history/', script)
        self.assertIn('fetch("jobs/translate-file"', script)
        self.assertIn('document.getElementById("inputTabSelect").addEventListener("change"', script)
        self.assertIn('document.getElementById("clearInput").addEventListener("click", clearCurrentWork)', script)
        self.assertIn('download.href = "history/" + item.id + "/export?format="', script)
        self.assertIn('historyFormats.push("original")', script)
        self.assertIn("linguinator_ui_language", script)

    def test_frontend_script_starts_with_valid_javascript(self):
        script = (Path(main.APP_DIR) / "static" / "app.js").read_text(encoding="utf-8")

        self.assertTrue(script.lstrip().startswith("let languageData = null;"))
        self.assertNotIn("gerade    let languageData = null;", script)

    def test_frontend_language_flags_fall_back_to_globe_for_unknown_languages(self):
        script = (Path(main.APP_DIR) / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn('if (!countryCode || countryCode === "UN") return "🌐";', script)

    def test_translate_one_uses_lazy_loaded_torch_module(self):
        with patch.object(main, "load_model", return_value=(FakeTokenizer(), FakeModel(), "cpu", FakeTorch())):
            translated = main.translate_one("Hello", "eng_Latn", "deu_Latn")

        self.assertEqual(translated, "Uebersetzt")

    def test_normalize_translated_text_inserts_missing_sentence_space(self):
        # OPUS-MT's detokenizer occasionally drops the space after sentence-ending punctuation.
        self.assertEqual(
            main.normalize_translated_text("teilen.Wenn Sie die Option aktivieren"),
            "teilen. Wenn Sie die Option aktivieren",
        )
        self.assertEqual(main.normalize_translated_text("Ist es 3.5 Meter?"), "Ist es 3.5 Meter?")
        self.assertEqual(main.normalize_translated_text("Wirklich!Ja klar."), "Wirklich! Ja klar.")

    def test_translate_one_normalizes_missing_sentence_space(self):
        tokenizer = FakeTokenizer()
        tokenizer.batch_decode = lambda generated, skip_special_tokens: ["teilen.Wenn Sie"]

        with patch.object(main, "load_model", return_value=(tokenizer, FakeModel(), "cpu", FakeTorch())):
            translated = main.translate_one("Hello", "eng_Latn", "deu_Latn")

        self.assertEqual(translated, "teilen. Wenn Sie")

    def test_model_family_classifies_known_model_ids(self):
        self.assertEqual(main.model_family("Helsinki-NLP/opus-mt-tc-bible-big-mul-mul"), "prefix")
        self.assertEqual(main.model_family("Helsinki-NLP/opus-mt-en-de"), "plain")

    def test_model_language_code_per_family(self):
        self.assertEqual(main.model_language_code("Helsinki-NLP/opus-mt-tc-bible-big-mul-mul", "deu_Latn"), ">>deu<<")
        self.assertEqual(main.model_language_code("Helsinki-NLP/opus-mt-en-de", "deu_Latn"), "deu_Latn")

    def test_prepare_translation_prefix_prepends_target_token(self):
        tokenizer = FakeTokenizer()

        inputs, generate_kwargs = main.prepare_translation(
            tokenizer, "Helsinki-NLP/opus-mt-tc-bible-big-mul-mul", "Hello", "eng_Latn", "deu_Latn"
        )

        self.assertEqual(inputs["text"], ">>deu<< Hello")
        self.assertEqual(generate_kwargs, {})
        self.assertEqual(inputs["max_length"], main.TRANSLATE_MAX_TOKENS)

    def test_prepare_translation_plain_sends_text_unchanged(self):
        tokenizer = FakeTokenizer()

        inputs, generate_kwargs = main.prepare_translation(
            tokenizer, "Helsinki-NLP/opus-mt-en-de", "Hello", "eng_Latn", "deu_Latn"
        )

        self.assertEqual(inputs["text"], "Hello")
        self.assertEqual(generate_kwargs, {})

    def test_prepare_translation_caps_input_length_even_when_tokenizer_ignores_truncation(self):
        # opus-mt-en-de/de-en leave model_max_length unset in their tokenizer config, which
        # makes truncation=True alone a no-op; prepare_translation must cap explicitly or a
        # long paragraph overruns the model's position embeddings ("index out of range in self").
        tokenizer = FakeTokenizer()

        inputs, _ = main.prepare_translation(
            tokenizer, "Helsinki-NLP/opus-mt-en-de", "Hello " * 2000, "eng_Latn", "deu_Latn"
        )

        self.assertEqual(inputs["max_length"], main.TRANSLATE_MAX_TOKENS)
        self.assertTrue(inputs["truncation"])

    def test_iso_639_1_looks_up_core_languages(self):
        self.assertEqual(main.iso_639_1("deu_Latn"), "de")
        self.assertEqual(main.iso_639_1("eng_Latn"), "en")

    def test_resolve_model_prefers_dedicated_pair_over_fallback(self):
        with patch.object(main, "OPUS_PAIRS", {"en>de": {"model_id": "Helsinki-NLP/opus-mt-en-de", "license": "cc-by-4.0"}}):
            model_id, dedicated = main.resolve_model("eng_Latn", "deu_Latn")

        self.assertEqual(model_id, "Helsinki-NLP/opus-mt-en-de")
        self.assertTrue(dedicated)

    def test_resolve_model_falls_back_when_pair_not_curated(self):
        with patch.object(main, "OPUS_PAIRS", {}):
            model_id, dedicated = main.resolve_model("eng_Latn", "rus_Cyrl")

        self.assertEqual(model_id, main.FALLBACK_MODEL_ID)
        self.assertFalse(dedicated)

    def test_translate_one_resolves_model_per_pair(self):
        seen_model_ids = []

        def fake_load_model(model_id):
            seen_model_ids.append(model_id)
            return FakeTokenizer(), FakeModel(), "cpu", FakeTorch()

        with patch.object(main, "OPUS_PAIRS", {"en>de": {"model_id": "Helsinki-NLP/opus-mt-en-de", "license": "x"}}):
            with patch.object(main, "load_model", fake_load_model):
                main.translate_one("Hello", "eng_Latn", "deu_Latn")

        self.assertEqual(seen_model_ids, ["Helsinki-NLP/opus-mt-en-de"])

    def test_language_codes_returns_core_languages(self):
        with patch.object(main, "CORE_LANGUAGES", {"en": "eng_Latn", "de": "deu_Latn"}):
            self.assertEqual(main.language_codes(), ["deu_Latn", "eng_Latn"])

    def test_model_idle_unload_clears_cached_model_after_timeout(self):
        fake_torch = FakeTorchWithCuda()
        model_loader = FakeCachedLoader((FakeTokenizer(), FakeModel(), "cuda", fake_torch))
        tokenizer_loader = FakeCachedLoader(FakeTokenizer())

        with patch.object(main, "load_model", model_loader):
            with patch.object(main, "load_tokenizer", tokenizer_loader):
                with patch.dict(sys.modules, {"torch": fake_torch}):
                    with patch.object(main, "MODEL_IDLE_UNLOAD_ENABLED", True):
                        with patch.object(main, "MODEL_IDLE_SECONDS", 1200):
                            with patch.object(main, "MODEL_ACTIVE_USERS", 0):
                                with patch.object(main, "MODEL_LAST_USED", 100.0):
                                    unloaded = main.unload_model_if_idle(now=1301.0)

        self.assertTrue(unloaded)
        self.assertTrue(model_loader.cleared)
        self.assertTrue(tokenizer_loader.cleared)
        self.assertTrue(fake_torch.cuda.emptied)

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
            with patch.object(main, "ocr_pdf_page", return_value=""):
                markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("# Page 1", markdown)
        self.assertIn("Hello PDF", markdown)
        self.assertIn("# Page 2", markdown)
        self.assertIn("No extractable text found", markdown)

    def test_pdf_extraction_marks_low_text_pages(self):
        reader = FakeReader([FakePage("tiny")])

        with patch.object(main, "PdfReader", return_value=reader):
            with patch.object(main, "ocr_pdf_page", return_value=""):
                markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("tiny", markdown)

    def test_pdf_extraction_uses_page_range(self):
        reader = FakeReader([FakePage("Page one long text, well over the OCR threshold"), FakePage("Page two long text, well over the OCR threshold")])

        with patch.object(main, "PdfReader", return_value=reader):
            markdown = main.extract_pdf_markdown_from_bytes(b"%PDF", page_range="2")

        self.assertNotIn("# Page 1", markdown)
        self.assertIn("# Page 2", markdown)

    def test_pdf_extraction_reports_scanned_pdf(self):
        reader = FakeReader([FakePage(""), FakePage(None)])

        with patch.object(main, "PdfReader", return_value=reader):
            with patch.object(main, "ocr_pdf_page", return_value=""):
                with self.assertRaises(HTTPException) as raised:
                    main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("no readable text was produced", raised.exception.detail)

    def test_ensure_ocr_tools_reports_rebuild_hint_when_tools_are_missing(self):
        with patch.object(main.shutil, "which", side_effect=lambda tool: None):
            with self.assertRaises(HTTPException) as raised:
                main.ensure_ocr_tools()

        self.assertEqual(raised.exception.status_code, 500)
        self.assertIn("Rebuild or restart from a current image that includes the OCR binaries", raised.exception.detail)

    def test_pdf_extraction_uses_ocr_for_empty_pages(self):
        reader = FakeReader([FakePage("")])

        with patch.object(main, "PdfReader", return_value=reader):
            with patch.object(main, "ocr_pdf_page", return_value="OCR text") as mocked_ocr:
                markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("OCR text", markdown)
        mocked_ocr.assert_called_once_with(b"%PDF", 1)

    def test_create_text_pdf_returns_pdf_document(self):
        content = main.create_text_pdf("Translated text\n\nSecond paragraph")

        self.assertTrue(content.startswith(b"%PDF-1.4"))
        self.assertIn(b"/Type /Page", content)
        self.assertIn("Translated text", pdf_text(content))

    def test_create_text_pdf_preserves_markdown_page_breaks(self):
        content = main.create_text_pdf("# Page 1\n\nFirst\n\n# Page 2\n\nSecond")
        text = pdf_text(content)

        self.assertIn(b"/Count 2", content)
        self.assertIn("Page 1", text)
        self.assertIn("Page 2", text)

    def test_create_text_pdf_formats_markdown_headings(self):
        content = main.create_text_pdf("# Title\n\nBody")

        self.assertIn(b"/F2 15 Tf", content)
        self.assertIn("Title", pdf_text(content))

    def test_create_text_pdf_embeds_font_for_non_latin_text(self):
        if not main.load_embedded_font(False):
            self.skipTest("no TrueType font available on this machine")
        content = main.create_text_pdf("Privet mir: Привет")

        self.assertIn(b"/FontFile2", content)
        # Round-trips through the ToUnicode CMap, so the text stays selectable.
        self.assertIn("Привет", pdf_text(content))

    def test_create_text_docx_returns_valid_package(self):
        content = main.create_text_docx("# Title\n\nHallo Welt.\n\nSecond paragraph.")

        with zipfile.ZipFile(BytesIO(content)) as docx:
            names = docx.namelist()
            self.assertIn("[Content_Types].xml", names)
            self.assertIn("_rels/.rels", names)
            self.assertIn("word/document.xml", names)
            document = ElementTree.fromstring(docx.read("word/document.xml"))

        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = [
            "".join(t.text or "" for t in p.findall("w:r/w:t", ns))
            for p in document.findall(".//w:p", ns)
        ]
        self.assertIn("Title", paragraphs)
        self.assertIn("Hallo Welt.", paragraphs)
        self.assertIn("Second paragraph.", paragraphs)

    def test_create_text_docx_marks_headings_bold(self):
        content = main.create_text_docx("# Title\n\nBody")

        with zipfile.ZipFile(BytesIO(content)) as docx:
            document = ElementTree.fromstring(docx.read("word/document.xml"))

        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        bold_runs = [run for run in document.findall(".//w:r", ns) if run.find("w:rPr/w:b", ns) is not None]
        self.assertEqual(len(bold_runs), 1)
        self.assertEqual(bold_runs[0].find("w:t", ns).text, "Title")

    def test_create_text_docx_escapes_xml_special_characters(self):
        content = main.create_text_docx("Cats & dogs <together>")

        with zipfile.ZipFile(BytesIO(content)) as docx:
            document = ElementTree.fromstring(docx.read("word/document.xml"))  # raises if malformed

        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        self.assertEqual(document.find(".//w:t", ns).text, "Cats & dogs <together>")

    def test_export_doc_route_returns_docx(self):
        response = TestClient(main.app).post("/export-doc", json={"text": "Hallo Welt."})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        with zipfile.ZipFile(BytesIO(response.content)) as docx:
            self.assertIn("word/document.xml", docx.namelist())

    def test_export_doc_route_rejects_empty_text(self):
        response = TestClient(main.app).post("/export-doc", json={"text": "   "})

        self.assertEqual(response.status_code, 400)

    def test_export_original_history_content_exports_docx(self):
        with patch.object(main, "export_docx_with_translated_text", return_value=b"translated docx") as export_mock:
            result = main.export_original_history_content("docx", b"source docx", "translated text", {})

        export_mock.assert_called_once_with(b"source docx", "translated text")
        self.assertEqual(result, b"translated docx")

    def test_export_original_history_content_uses_layout_overlay_when_marked(self):
        with patch.object(main, "export_pdf_layout_with_translated_text", return_value=b"overlay pdf") as overlay_mock:
            result = main.export_original_history_content("pdf", b"source pdf", "text", {"layout": "true"})

        overlay_mock.assert_called_once_with(b"source pdf", "text")
        self.assertEqual(result, b"overlay pdf")

    def test_group_pdf_lines_merges_runs_on_the_same_baseline(self):
        runs = [
            {"text": "Hello", "x": 50.0, "y": 700.0, "size": 11.0},
            {"text": "world", "x": 90.0, "y": 700.2, "size": 11.0},
            {"text": "Second", "x": 50.0, "y": 686.0, "size": 11.0},
        ]

        lines = main.group_pdf_lines(runs)

        self.assertEqual([line["text"] for line in lines], ["Hello world", "Second"])
        self.assertGreater(lines[0]["right"], 90.0)

    def test_group_pdf_lines_accumulates_runs_reported_at_the_same_position(self):
        # Runs drawn in one text block all report the position of the block, so the second run
        # has to continue from the end of the first instead of restarting there.
        runs = [
            {"text": "Hello", "x": 50.0, "y": 700.0, "size": 11.0, "width": 30.0},
            {"text": "world", "x": 50.0, "y": 700.0, "size": 11.0, "width": 30.0},
        ]

        lines = main.group_pdf_lines(runs)

        self.assertEqual(lines[0]["text"], "Helloworld")
        self.assertAlmostEqual(lines[0]["right"], 110.0)

    def test_group_pdf_paragraphs_splits_on_gaps_and_indentation(self):
        lines = [
            {"text": "One", "x": 50.0, "y": 700.0, "right": 100.0, "size": 11.0},
            {"text": "still one", "x": 50.0, "y": 686.0, "right": 100.0, "size": 11.0},
            {"text": "far below", "x": 50.0, "y": 600.0, "right": 100.0, "size": 11.0},
            {"text": "indented", "x": 90.0, "y": 586.0, "right": 140.0, "size": 11.0},
        ]

        paragraphs = main.group_pdf_paragraphs(lines)

        self.assertEqual([paragraph["text"] for paragraph in paragraphs],
                         ["One still one", "far below", "indented"])

    def test_reflow_paragraph_shrinks_instead_of_truncating(self):
        paragraph = {"lines": [
            {"text": "Short", "x": 50.0, "y": 700.0, "right": 200.0, "size": 11.0},
            {"text": "line", "x": 50.0, "y": 686.0, "right": 200.0, "size": 11.0},
        ]}
        long_text = "Eine deutlich laengere Uebersetzung " * 6

        covers, placed = main.reflow_paragraph(paragraph, long_text)

        self.assertEqual(len(covers), 2)
        # Shrunk, but never below the floor, and never dropping lines to make it fit.
        self.assertLess(placed[0]["size"], 11.0)
        self.assertGreaterEqual(placed[0]["size"], 11.0 * main.PDF_LAYOUT_MIN_SCALE)
        self.assertGreater(len(placed), 2)
        self.assertEqual(" ".join(line["text"] for line in placed).split(), long_text.split())

    def test_wrap_text_to_width_keeps_every_word(self):
        text = "eins zwei drei vier fuenf sechs sieben acht"

        wrapped = main.wrap_text_to_width(text, 60.0, 11.0)

        self.assertGreater(len(wrapped), 1)
        self.assertEqual(" ".join(wrapped).split(), text.split())

    def test_pdf_layout_roundtrip_replaces_text_and_keeps_page_size(self):
        source = main.create_pdf_from_pages([{
            "width": 400, "height": 300, "margin": 40,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [
                {"text": "Hello world", "font": "F1", "size": 11, "line_height": 14},
                {"text": "second line", "font": "F1", "size": 11, "line_height": 14},
            ],
        }])

        pages = main.extract_pdf_layout(source)
        self.assertEqual(len(pages), 1)
        self.assertAlmostEqual(pages[0]["width"], 400, places=1)
        self.assertIn("Hello world", pages[0]["paragraphs"][0]["text"])

        overlay = main.export_pdf_layout_with_translated_text(
            source,
            "\n\n".join(paragraph["text"].replace("Hello", "Hallo") for paragraph in pages[0]["paragraphs"]),
        )
        text = pdf_text(overlay)
        self.assertIn("Hallo", text)

    def test_run_pdf_layout_translate_job_overlays_original_pdf(self):
        source = main.create_pdf_from_pages([{
            "width": 400, "height": 300, "margin": 40,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [{"text": "Hello world", "font": "F1", "size": 11, "line_height": 14}],
        }])
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "HISTORY_DIR", temp_dir):
                with patch.object(main, "JOBS_DIR", temp_dir / "jobs"):
                    with main.JOBS_LOCK:
                        main.JOBS.clear()
                        main.JOB_RUNNERS.clear()
                    job_id = main.create_job("translate-pdf-layout", "eng_Latn", "deu_Latn", "input.pdf")
                    with patch.object(main, "translate_one", return_value="Hallo Welt"):
                        main.run_pdf_layout_translate_job(job_id, source, "eng_Latn", "deu_Latn", "input.pdf")

                    job = main.get_job(job_id)
                    history = main.history_item(job["history_id"])
                    exported = main.export_original_history_content(
                        "pdf", source, job["result"], history["source_meta"]
                    )

            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["result"], "Hallo Welt")
            self.assertEqual(history["source_extension"], "pdf")
            self.assertEqual(history["source_meta"], {"layout": "true"})
            self.assertIn("Hallo Welt", pdf_text(exported))
        finally:
            with main.JOBS_LOCK:
                main.JOBS.clear()
                main.JOB_RUNNERS.clear()
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_pdf_text_object_encodes_unicode(self):
        with patch.object(main, "load_embedded_font", return_value=None):
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

    def test_history_export_supports_text_and_pdf(self):
        temp_dir = test_temp_dir()
        try:
            item_id = "2026-08-02-text-abc123"
            md_path = temp_dir / f"{item_id}.md"
            md_path.write_text("Translated result", encoding="utf-8")

            with patch.object(main, "HISTORY_DIR", temp_dir):
                client = TestClient(main.app)
                text_response = client.get(f"/history/{item_id}/export?format=txt")
                pdf_response = client.get(f"/history/{item_id}/export?format=pdf")
                doc_response = client.get(f"/history/{item_id}/export?format=doc")
                bad_response = client.get(f"/history/{item_id}/export?format=docx")

            self.assertEqual(text_response.status_code, 200)
            self.assertIn("text/plain", text_response.headers["content-type"])
            self.assertEqual(text_response.text, "Translated result")
            self.assertEqual(pdf_response.status_code, 200)
            self.assertEqual(pdf_response.headers["content-type"], "application/pdf")
            self.assertTrue(pdf_response.content.startswith(b"%PDF-1.4"))
            self.assertEqual(doc_response.status_code, 200)
            self.assertEqual(
                doc_response.headers["content-type"],
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            with zipfile.ZipFile(BytesIO(doc_response.content)) as docx:
                self.assertIn("word/document.xml", docx.namelist())
            self.assertEqual(bad_response.status_code, 404)
        finally:
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_save_history_includes_a_short_disambiguation_code(self):
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "HISTORY_DIR", temp_dir):
                item_id = main.save_history("text", "Hallo", "eng_Latn", "deu_Latn", "notes.txt")
                items = main.history_items()

            self.assertEqual(len(items[0]["code"]), 8)
            self.assertTrue(item_id.endswith(items[0]["code"]))
        finally:
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_history_export_uses_stored_source_file_for_original_format(self):
        temp_dir = test_temp_dir()
        try:
            source = minimal_docx(["Hello"])
            with patch.object(main, "HISTORY_DIR", temp_dir):
                item_id = main.save_history(
                    "text",
                    "Hallo",
                    "eng_Latn",
                    "deu_Latn",
                    "source.docx",
                    source,
                    "docx",
                )
                client = TestClient(main.app)
                response = client.get(f"/history/{item_id}/export?format=original")
                items = main.history_items()

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers["content-type"],
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            self.assertEqual(main.extract_docx_text_from_bytes(response.content), "Hallo")
            self.assertTrue(items[0]["has_source_file"])
            self.assertEqual(items[0]["source_extension"], "docx")
        finally:
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_delete_history_removes_stored_source_file(self):
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "HISTORY_DIR", temp_dir):
                item_id = main.save_history(
                    "text",
                    "Hallo",
                    "eng_Latn",
                    "deu_Latn",
                    "source.docx",
                    minimal_docx(["Hello"]),
                    "docx",
                )
                source_path = main.history_source_path(item_id, "docx")
                response = TestClient(main.app).delete(f"/history/{item_id}")

            self.assertEqual(response.status_code, 200)
            self.assertFalse(source_path.exists())
        finally:
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_list_jobs_hides_payloads_and_adds_queue_positions(self):
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "JOBS_DIR", temp_dir):
                with main.JOBS_LOCK:
                    main.JOBS.clear()
                    main.JOB_RUNNERS.clear()
                first = main.create_job("translate", "eng_Latn", "deu_Latn", "Text")
                second = main.create_job("translate-pdf", "eng_Latn", "deu_Latn", "file.pdf")
                main.update_job(first, text="secret source")
                main.update_job(second, payload_path=str(temp_dir / "payload.bin"), source_payload_path=str(temp_dir / "source.bin"))

                items = main.list_jobs()

            self.assertEqual([item["position"] for item in items], [1, 2])
            self.assertNotIn("text", items[0])
            self.assertNotIn("payload_path", items[1])
            self.assertNotIn("source_payload_path", items[1])
        finally:
            with main.JOBS_LOCK:
                main.JOBS.clear()
                main.JOB_RUNNERS.clear()
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_get_job_recovers_completed_result_from_history(self):
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "HISTORY_DIR", temp_dir):
                with patch.object(main, "JOBS_DIR", temp_dir / "jobs"):
                    with main.JOBS_LOCK:
                        main.JOBS.clear()
                        main.JOB_RUNNERS.clear()
                    history_id = main.save_history("text", "Recovered", "eng_Latn", "deu_Latn", "text")
                    job_id = main.create_job("translate", "eng_Latn", "deu_Latn", "Text")
                    main.update_job(job_id, status="complete", result=None, history_id=history_id)

                    job = main.get_job(job_id)

            self.assertEqual(job["result"], "Recovered")
        finally:
            with main.JOBS_LOCK:
                main.JOBS.clear()
                main.JOB_RUNNERS.clear()
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_control_queued_job_cancels_without_worker(self):
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "JOBS_DIR", temp_dir):
                with main.JOBS_LOCK:
                    main.JOBS.clear()
                    main.JOB_RUNNERS.clear()
                job_id = main.create_job("translate", "eng_Latn", "deu_Latn", "Text")
                payload_path = temp_dir / "queued.source"
                payload_path.write_text("source", encoding="utf-8")
                main.update_job(job_id, source_payload_path=str(payload_path), text="secret")

                job = main.control_job(job_id, "cancel")

            self.assertEqual(job["status"], "cancelled")
            self.assertTrue(job["cancel_requested"])
            self.assertFalse(payload_path.exists())
        finally:
            with main.JOBS_LOCK:
                main.JOBS.clear()
                main.JOB_RUNNERS.clear()
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_load_persisted_jobs_requeues_running_jobs(self):
        temp_dir = test_temp_dir()
        try:
            job_id = "job-1"
            (temp_dir / f"{job_id}.json").write_text(json.dumps({
                "id": job_id,
                "kind": "translate",
                "status": "running",
                "message": "Running",
                "source": "eng_Latn",
                "target": "deu_Latn",
                "queued_at": 100.0,
                "started_at": 101.0,
            }), encoding="utf-8")

            with patch.object(main, "JOBS_DIR", temp_dir):
                with main.JOBS_LOCK:
                    main.JOBS.clear()
                    main.JOB_RUNNERS.clear()
                main.load_persisted_jobs()
                loaded = main.JOBS[job_id]

            self.assertEqual(loaded["status"], "queued")
            self.assertEqual(loaded["message"], "Requeued after restart")
            self.assertIsNone(loaded["started_at"])
        finally:
            with main.JOBS_LOCK:
                main.JOBS.clear()
                main.JOB_RUNNERS.clear()
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_configure_torch_threads_uses_env_values(self):
        torch_module = FakeTorchWithThreads()

        with patch.object(main, "CPU_THREADS", 3):
            with patch.object(main, "CPU_INTEROP_THREADS", 2):
                main.configure_torch_threads(torch_module)

        self.assertEqual(torch_module.threads, 3)
        self.assertEqual(torch_module.interop_threads, 2)

    def test_languages_endpoint_returns_core_languages_without_loading_a_model(self):
        client = TestClient(main.app)

        with patch.object(main, "load_tokenizer", side_effect=RuntimeError("must not load a model")):
            response = client.get("/languages")

        self.assertEqual(response.status_code, 200)
        codes = [item["code"] for item in response.json()["languages"]]
        self.assertIn("eng_Latn", codes)
        self.assertIn("deu_Latn", codes)

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
