import base64
import json
import os
import shutil
import socket
import sys
import time
import unicodedata
import unittest
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree

import pymupdf
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main
import app.server as server


def pdf_spans(content, page_number=0):
    """Every drawn span of a generated PDF, read back the way a viewer sees it. Asserted on
    rather than the byte stream: MuPDF writes the file, its exact operators are its own
    business."""
    page = pymupdf.open(stream=content, filetype="pdf")[page_number]
    return [span for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", []) for span in line["spans"]]


def pdf_page_count(content):
    return len(pymupdf.open(stream=content, filetype="pdf"))


def pdf_text(content):
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)


class FakePage:
    def __init__(self, text):
        self.text = text

    def get_text(self, kind="text"):
        return self.text


class FakeDocument:
    """Stand-in for a pymupdf.Document; the extractor only counts and indexes pages."""

    def __init__(self, pages):
        self.pages = pages
        self.page_count = len(pages)

    def __getitem__(self, index):
        return self.pages[index]


def fold(text):
    """NFKC, so a letter and the presentation form of it compare equal."""
    return unicodedata.normalize("NFKC", text)


def mupdf_span(text, x, y, size=11.0, bold=False, direction=(1.0, 0.0), font="Helvetica",
               draw_order=None):
    """One span as PyMuPDF reports it: baseline origin and bbox in MuPDF's top-down space.

    Characters are placed left to right in the order of `text`; `draw_order` hands over the
    same characters in the order the page paints them, which is what a right-to-left producer
    reorders.
    """
    width = 0.5 * size * len(text)
    characters = [{"c": char, "origin": (x + 0.5 * size * index, y)}
                  for index, char in enumerate(text)]
    if draw_order:
        remaining = list(characters)
        characters = []
        for char in draw_order:
            entry = next(item for item in remaining if item["c"] == char)
            remaining.remove(entry)
            characters.append(entry)
    return {
        "text": text,
        "origin": (x, y),
        "bbox": (x, y - size, x + width, y + 0.2 * size),
        "size": size,
        "flags": main.MUPDF_BOLD_FLAG if bold else 0,
        "dir": direction,
        "font": font,
        "chars": characters,
    }


class FakeMuPdfPage:
    """Shaped like a PyMuPDF page: one line per span, plus the page's y-flip matrix."""

    def __init__(self, spans, height=800.0):
        self.spans = spans
        self.transformation_matrix = pymupdf.Matrix(1, 0, 0, -1, 0, height)

    def get_text(self, kind):
        lines = [{"dir": span["dir"], "spans": [span]} for span in self.spans]
        return {"blocks": [{"lines": lines}]}


class FakeInputs(dict):
    def to(self, device):
        self["device"] = device
        return self


class FakeTokenizer:
    def __init__(self):
        self.src_lang = None

    def __call__(self, text, return_tensors, truncation, max_length=None, padding=False):
        return FakeInputs({"text": text, "return_tensors": return_tensors, "truncation": truncation, "max_length": max_length, "padding": padding})

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

    def test_health_reports_time_format(self):
        with patch.object(main, "TIME_FORMAT", "24h"):
            response = TestClient(main.app).get("/health")

        self.assertEqual(response.json()["time_format"], "24h")

    def test_health_reports_proxy_configuration(self):
        with patch.object(main, "ROOT_PATH", "/linguinator"):
            response = TestClient(main.app).get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["root_path"], "/linguinator")

    def test_root_path_is_normalized_for_reverse_proxy_prefixes(self):
        self.assertEqual(main.normalized_root_path("linguinator"), "/linguinator")
        self.assertEqual(main.normalized_root_path("/linguinator/"), "/linguinator")
        self.assertEqual(main.normalized_root_path(""), "")

    def test_env_value_prefers_set_variable_over_default(self):
        with patch.dict(os.environ, {"LINGUINATOR_MODEL": "new"}, clear=False):
            self.assertEqual(main.env_value("LINGUINATOR_MODEL", "default"), "new")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main.env_value("LINGUINATOR_MODEL", "default"), "default")

    def test_server_reads_host_and_port_env(self):
        env = {
            "LINGUINATOR_HOST": "127.0.0.1",
            "LINGUINATOR_PORT": "5443",
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
        # Direct HTTPS is gone: TLS belongs to the reverse proxy.
        self.assertNotIn("ssl_certfile", options)

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
        self.assertIn('"history/" + item.id + "/export?format="', script)
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

    def test_translate_batch_maps_results_back_by_position_and_skips_empties(self):
        seen_texts = {}

        class RecordingTokenizer(FakeTokenizer):
            def __call__(self, text, return_tensors, truncation, max_length=None, padding=False):
                seen_texts["text"] = text
                seen_texts["padding"] = padding
                return FakeInputs({"text": text})

            def batch_decode(self, generated, skip_special_tokens):
                return ["Eins", "Zwei"]

        model = FakeModel()
        model.generate = lambda **inputs: ["g1", "g2"]

        with patch.object(main, "load_model", return_value=(RecordingTokenizer(), model, "cpu", FakeTorch())):
            translated = main.translate_batch(["Hello", "", "World"], "eng_Latn", "deu_Latn")

        self.assertEqual(translated, ["Eins", "", "Zwei"])
        self.assertEqual(seen_texts["text"], ["Hello", "World"])
        self.assertTrue(seen_texts["padding"])

    def test_translate_batch_empty_input_skips_model_call(self):
        with patch.object(main, "load_model", side_effect=AssertionError("should not load a model")):
            translated = main.translate_batch(["", ""], "eng_Latn", "deu_Latn")

        self.assertEqual(translated, ["", ""])

    def test_model_family_classifies_known_model_ids(self):
        self.assertEqual(main.model_family("Helsinki-NLP/opus-mt-tc-bible-big-mul-mul"), "prefix")
        self.assertEqual(main.model_family("Helsinki-NLP/opus-mt-en-de"), "plain")

    def test_model_language_code_per_family(self):
        self.assertEqual(main.model_language_code("Helsinki-NLP/opus-mt-tc-bible-big-mul-mul", "deu_Latn"), ">>deu<<")
        self.assertEqual(main.model_language_code("Helsinki-NLP/opus-mt-en-de", "deu_Latn"), "deu_Latn")

    def test_model_language_code_maps_aliases_the_fallback_actually_knows(self):
        # The fallback's vocabulary has >>ara<< but no >>arb<<. An unknown prefix is silently
        # tokenised as plain text rather than rejected, leaving the model without a target
        # signal: nl>ar came back in Korean before this mapping existed.
        self.assertEqual(main.model_language_code("Helsinki-NLP/opus-mt-tc-bible-big-mul-mul", "arb_Arab"), ">>ara<<")
        # A dedicated bilingual model takes the internal code unchanged, aliases included.
        self.assertEqual(main.model_language_code("Helsinki-NLP/opus-mt-de-ar", "arb_Arab"), "arb_Arab")

    def test_every_core_language_has_a_prefix_token_alias_entry_or_matches_directly(self):
        # Guard for the next language added: its short code must either be one the fallback
        # already knows or be mapped in FALLBACK_LANGUAGE_ALIASES. The vocabulary itself cannot
        # be checked here (that needs the real model), so this only asserts the mapping is
        # applied - the vocabulary check is the manual step documented alongside the alias table.
        for internal in main.CORE_LANGUAGES.values():
            with self.subTest(language=internal):
                token = main.model_language_code(main.FALLBACK_MODEL_ID, internal)
                self.assertTrue(token.startswith(">>") and token.endswith("<<"))
                short = internal.split("_", 1)[0]
                self.assertEqual(token, f">>{main.FALLBACK_LANGUAGE_ALIASES.get(short, short)}<<")

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
                    with patch.object(main, "MODEL_IDLE_SECONDS", 1200):
                        if True:
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
                with patch.object(main, "MODEL_IDLE_SECONDS", 1200):
                    with patch.object(main, "MODEL_ACTIVE_USERS", 0):
                        with patch.object(main, "MODEL_LAST_USED", 100.0):
                            unloaded = main.unload_model_if_idle(now=1000.0)

        self.assertFalse(unloaded)
        self.assertFalse(model_loader.cleared)
        self.assertFalse(tokenizer_loader.cleared)

    def test_model_idle_unload_skips_when_switched_off_or_active(self):
        model_loader = FakeCachedLoader((FakeTokenizer(), FakeModel(), "cpu", FakeTorch()))
        tokenizer_loader = FakeCachedLoader(FakeTokenizer())

        with patch.object(main, "load_model", model_loader):
            with patch.object(main, "load_tokenizer", tokenizer_loader):
                with patch.object(main, "MODEL_IDLE_SECONDS", 0):
                    self.assertFalse(main.unload_model_if_idle(now=2000.0))
                with patch.object(main, "MODEL_IDLE_SECONDS", 1200):
                    with patch.object(main, "MODEL_ACTIVE_USERS", 1):
                        with patch.object(main, "MODEL_LAST_USED", 100.0):
                            self.assertFalse(main.unload_model_if_idle(now=2000.0))

        self.assertFalse(model_loader.cleared)
        self.assertFalse(tokenizer_loader.cleared)

    def test_pdf_extraction_marks_empty_pages(self):
        document = FakeDocument([FakePage("Hello PDF\n"), FakePage("")])

        with patch.object(main.pymupdf, "open", return_value=document):
            with patch.object(main, "ocr_pdf_page", return_value=""):
                markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("# Page 1", markdown)
        self.assertIn("Hello PDF", markdown)
        self.assertIn("# Page 2", markdown)
        self.assertIn("No extractable text found", markdown)

    def test_pdf_extraction_marks_low_text_pages(self):
        document = FakeDocument([FakePage("tiny")])

        with patch.object(main.pymupdf, "open", return_value=document):
            with patch.object(main, "ocr_pdf_page", return_value=""):
                markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("tiny", markdown)

    def test_pdf_extraction_uses_page_range(self):
        document = FakeDocument([FakePage("Page one long text, well over the OCR threshold"), FakePage("Page two long text, well over the OCR threshold")])

        with patch.object(main.pymupdf, "open", return_value=document):
            markdown = main.extract_pdf_markdown_from_bytes(b"%PDF", page_range="2")

        self.assertNotIn("# Page 1", markdown)
        self.assertIn("# Page 2", markdown)

    def test_ensure_public_url_rejects_addresses_inside_the_network(self):
        # Whoever reaches the UI decides what the container fetches. Without this, that includes
        # the router, a sibling container, and the app's own endpoints - returned as a tidy PDF.
        blocked = {
            "http://localhost:5051/health": "127.0.0.1",
            "http://127.0.0.1/": "127.0.0.1",
            "http://10.0.0.5/admin": "10.0.0.5",
            "http://192.168.2.1/": "192.168.2.1",
            "http://172.17.0.2/": "172.17.0.2",
            "http://169.254.169.254/latest/meta-data/": "169.254.169.254",
            "http://nas.local/": "192.168.2.50",
        }
        for url, address in blocked.items():
            with self.subTest(url=url):
                resolved = [(socket.AF_INET, None, None, "", (address, 80))]
                with patch.object(main.socket, "getaddrinfo", return_value=resolved):
                    with self.assertRaises(HTTPException) as raised:
                        main.ensure_public_url(url)
                self.assertEqual(raised.exception.status_code, 400)

    def test_ensure_public_url_rejects_other_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com", "javascript:alert(1)"):
            with self.subTest(url=url):
                with self.assertRaises(HTTPException):
                    main.ensure_public_url(url)

    def test_ensure_public_url_accepts_a_public_address(self):
        resolved = [(socket.AF_INET, None, None, "", ("93.184.216.34", 443))]
        with patch.object(main.socket, "getaddrinfo", return_value=resolved):
            self.assertEqual(main.ensure_public_url("  https://example.com/a  "), "https://example.com/a")

    def test_ensure_public_url_checks_every_resolved_address(self):
        # A name can answer with both a public and a private address; one bad answer is enough.
        resolved = [
            (socket.AF_INET, None, None, "", ("93.184.216.34", 80)),
            (socket.AF_INET, None, None, "", ("127.0.0.1", 80)),
        ]
        with patch.object(main.socket, "getaddrinfo", return_value=resolved):
            with self.assertRaises(HTTPException):
                main.ensure_public_url("http://sneaky.example/")

    def test_fetch_web_page_rechecks_every_redirect(self):
        """A redirect to an internal address is the ordinary way past an address check.

        The public page answers "302 -> http://127.0.0.1:5051/health". Only re-checking each hop
        catches it, which is why redirects are followed by hand instead of by httpx.
        """
        class FakeResponse:
            def __init__(self, location):
                self.is_redirect = True
                self.status_code = 302
                self.headers = {"location": location}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stream(self, _method, _url, **_kwargs):
                return FakeResponse("http://127.0.0.1:5051/health")

        def fake_getaddrinfo(host, *_args, **_kwargs):
            address = "127.0.0.1" if host == "127.0.0.1" else "93.184.216.34"
            return [(socket.AF_INET, None, None, "", (address, 80))]

        with patch.object(main.httpx, "Client", FakeClient):
            with patch.object(main.socket, "getaddrinfo", side_effect=fake_getaddrinfo):
                with self.assertRaises(HTTPException) as raised:
                    main.fetch_web_page("http://example.com/redirects-inward")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("your own network", raised.exception.detail)

    def test_run_url_translate_job_keeps_headings_and_lists(self):
        # The markers must survive the model: a translated "## Abschnitt" that comes back without
        # its "##" lands in the finished PDF as ordinary body text.
        markdown = "# Titel\n\n## Abschnitt\n\nEin Absatz.\n\n- Erster Punkt"
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "HISTORY_DIR", temp_dir):
                with patch.object(main, "JOBS_DIR", temp_dir / "jobs"):
                    with main.JOBS_LOCK:
                        main.JOBS.clear()
                        main.JOB_RUNNERS.clear()
                    job_id = main.create_job("translate-url", "deu_Latn", "eng_Latn", "example.com")
                    with patch.object(main, "fetch_web_page", return_value=(b"<html></html>", "https://example.com/a")):
                        with patch.object(main, "extract_web_page_markdown", return_value=(markdown, "Titel")):
                            with patch.object(main, "translate_batch", side_effect=lambda texts, *_a: [f"EN {t}" for t in texts]):
                                main.run_url_translate_job(job_id, "https://example.com/a", "deu_Latn", "eng_Latn")
                    job = main.get_job(job_id)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertEqual(job["status"], "complete")
        self.assertEqual(
            job["result"],
            "# EN Titel\n\n## EN Abschnitt\n\nEN Ein Absatz.\n\n- EN Erster Punkt",
        )

    def test_split_markdown_blocks_keeps_markers_out_of_the_text(self):
        markdown = "# Titel\n\n## Abschnitt\n\nEin Absatz.\n- Erster Punkt\n2. Zweiter\n> Zitat"

        blocks = main.split_markdown_blocks(markdown)

        self.assertEqual(blocks[0], ("# ", "Titel"))
        self.assertEqual(blocks[2], ("## ", "Abschnitt"))
        self.assertEqual(blocks[4], ("", "Ein Absatz."))
        self.assertEqual(blocks[5], ("- ", "Erster Punkt"))
        self.assertEqual(blocks[6], ("2. ", "Zweiter"))
        self.assertEqual(blocks[7], ("> ", "Zitat"))
        # Reassembling must give back exactly what went in, or the document shifts.
        self.assertEqual("\n".join(prefix + text for prefix, text in blocks), markdown)

    def test_extract_web_page_markdown_keeps_the_structure(self):
        html = (
            "<html><head><title>Beispielseite</title></head><body>"
            "<nav>Startseite Kontakt Impressum</nav>"
            "<article><h1>Die Ueberschrift</h1>"
            "<p>Ein erster Absatz mit Inhalt der lang genug ist um erkannt zu werden.</p>"
            "<h2>Zweiter Abschnitt</h2><ul><li>Erster Punkt</li><li>Zweiter Punkt</li></ul>"
            "<p>Noch ein Absatz mit ausreichend Text damit die Heuristik ihn behaelt.</p>"
            "</article><footer>Copyright 2026</footer></body></html>"
        ).encode("utf-8")

        markdown, title = main.extract_web_page_markdown(html, "https://example.com/seite")

        self.assertIn("Die Ueberschrift", title)
        self.assertIn("## Zweiter Abschnitt", markdown)
        self.assertIn("- Erster Punkt", markdown)
        # Navigation and footer are what a reader view is for.
        self.assertNotIn("Impressum", markdown)
        self.assertNotIn("Copyright 2026", markdown)

    def test_extract_web_page_markdown_strips_leftover_html_and_emphasis(self):
        """Headings must survive, inline markup must not.

        The reader output leaves footnote markup like <sup>[1]</sup> in place, which prints
        verbatim in the PDF, and inline bold does not survive translation - the model returned
        "*Reproduzierbare Builds**" for "**Reproducible builds**".
        """
        html = (
            "<html><head><title>Seite</title></head><body><article>"
            "<h1>Die Ueberschrift</h1>"
            "<p><b>Fetter Anfang</b> und ein Absatz mit genug Inhalt damit er erkannt wird"
            " und eine Fussnote<sup>[1]</sup> traegt.</p>"
            "<h2>Zweiter Abschnitt</h2>"
            "<p>Noch ein Absatz mit ausreichend Text damit die Heuristik ihn behaelt und nicht verwirft.</p>"
            "</article></body></html>"
        ).encode("utf-8")

        markdown, _title = main.extract_web_page_markdown(html, "https://example.com/seite")

        self.assertIn("## Zweiter Abschnitt", markdown)   # structure kept
        self.assertNotIn("<sup>", markdown)               # raw html gone
        self.assertNotIn("**", markdown)                  # inline emphasis gone
        self.assertIn("Fetter Anfang", markdown)          # its text stays

    def test_extract_web_page_markdown_rejects_a_javascript_redirect_stub(self):
        # blog.rust-lang.org redirects with JavaScript: the fetched body is a stub that extracts
        # to a couple of dozen characters. Shipping that as a translation would be worse than
        # saying it did not work.
        html = (
            b"<!doctype html><meta charset='utf-8'><title>Redirect</title>"
            b"<script>window.location.replace('https://example.com/real');</script>"
        )

        with self.assertRaises(HTTPException) as raised:
            main.extract_web_page_markdown(html, "https://example.com/stub")

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("JavaScript", raised.exception.detail)

    def test_extract_web_page_markdown_explains_an_empty_page(self):
        # A page that builds itself with JavaScript must say so, not yield an empty document.
        html = b"<html><head><title>App</title></head><body><div id='root'></div></body></html>"

        with self.assertRaises(HTTPException) as raised:
            main.extract_web_page_markdown(html, "https://example.com/app")

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("JavaScript", raised.exception.detail)

    def test_parse_page_range_clamps_a_range_to_the_document_end(self):
        # "the first 5 pages" is a reasonable request for a shorter document; it used to be
        # rejected outright with "Page range must be between 1 and 1".
        self.assertEqual(main.parse_page_range("1-5", 1), [1])
        self.assertEqual(main.parse_page_range("1-5", 3), [1, 2, 3])
        self.assertEqual(main.parse_page_range("2-9", 4), [2, 3, 4])

    def test_parse_page_range_still_rejects_a_single_page_past_the_end(self):
        # A page named outright is a typo worth reporting, not something to silently drop.
        with self.assertRaises(HTTPException):
            main.parse_page_range("7", 3)
        with self.assertRaises(HTTPException):
            main.parse_page_range("1,7", 3)

    def test_parse_page_range_rejects_a_range_entirely_past_the_end(self):
        with self.assertRaises(HTTPException):
            main.parse_page_range("8-10", 3)

    def test_parse_page_range_keeps_rejecting_malformed_and_zero_input(self):
        for bad in ("abc", "3-1", "1-", "-2"):
            with self.subTest(page_range=bad):
                with self.assertRaises(HTTPException):
                    main.parse_page_range(bad, 5)
        with self.assertRaises(HTTPException):
            main.parse_page_range("0-2", 5)

    def test_pdf_extraction_reports_scanned_pdf(self):
        document = FakeDocument([FakePage(""), FakePage(None)])

        with patch.object(main.pymupdf, "open", return_value=document):
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
        document = FakeDocument([FakePage("")])

        with patch.object(main.pymupdf, "open", return_value=document):
            with patch.object(main, "ocr_pdf_page", return_value="OCR text") as mocked_ocr:
                markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("OCR text", markdown)
        mocked_ocr.assert_called_once_with(b"%PDF", 1, main.AUTO_SOURCE)

    def test_favorite_languages_cap_at_three_plus_english(self):
        self.assertEqual(
            main.parse_favorite_languages("deu_Latn,spa_Latn,fra_Latn"),
            ["deu_Latn", "spa_Latn", "fra_Latn", "eng_Latn"],
        )
        # A fourth entry is dropped, not shown alongside the always-present English.
        self.assertEqual(len(main.parse_favorite_languages("deu_Latn,spa_Latn,fra_Latn,ita_Latn")), 4)
        # Unknown codes, blanks and duplicates are ignored; English is never listed twice.
        self.assertEqual(
            main.parse_favorite_languages(" deu_Latn , hrv_Latn, deu_Latn ,eng_Latn"),
            ["deu_Latn", "eng_Latn"],
        )
        self.assertEqual(main.parse_favorite_languages(""), ["eng_Latn"])

    def test_languages_route_reports_the_favorites(self):
        payload = TestClient(main.app).get("/languages").json()

        self.assertEqual(payload["favorites"], main.FAVORITE_LANGUAGES)
        self.assertIn("eng_Latn", payload["favorites"])

    def test_ocr_language_code_maps_source_language_to_tesseract(self):
        with patch.object(main, "installed_ocr_languages", return_value=("deu", "eng", "fra", "ara", "chi_sim")):
            self.assertEqual(main.ocr_language_code("deu_Latn"), "deu")
            self.assertEqual(main.ocr_language_code("fra_Latn"), "fra")
            # Tesseract names these differently than we do.
            self.assertEqual(main.ocr_language_code("arb_Arab"), "ara")
            self.assertEqual(main.ocr_language_code("zho_Hans"), "chi_sim")
            self.assertEqual(main.ocr_language_code(main.AUTO_SOURCE), "eng")

    def test_ocr_language_code_falls_back_when_the_package_is_missing(self):
        # An unknown -l aborts tesseract and takes the whole job with it.
        with patch.object(main, "installed_ocr_languages", return_value=("deu", "eng")):
            self.assertEqual(main.ocr_language_code("swe_Latn"), "eng")

    def test_ocr_pdf_page_reads_in_the_jobs_source_language(self):
        completed = SimpleNamespace(stdout="scanned text")

        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "installed_ocr_languages", return_value=("deu", "eng", "rus")):
                with patch.object(main.subprocess, "run", return_value=completed) as mocked_run:
                    main.ocr_pdf_page(b"%PDF", 1, "rus_Cyrl")

        tesseract_call = mocked_run.call_args_list[-1].args[0]
        self.assertEqual(tesseract_call[-2:], ["-l", "rus"])

    def test_ocr_page_blocks_reads_the_layout_out_of_the_tsv(self):
        # level 2 is a block, level 5 a word; the words belong to whichever block number they
        # carry, and blocks come back in tesseract's reading order.
        tsv = "\n".join([
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
            "2\t1\t1\t0\t0\t0\t100\t50\t400\t30\t-1\t",
            "5\t1\t1\t1\t1\t1\t100\t50\t180\t30\t96\tÜberschrift",
            "2\t1\t2\t0\t0\t0\t100\t120\t900\t200\t-1\t",
            "5\t1\t2\t1\t1\t1\t100\t120\t100\t20\t95\tErster",
            "5\t1\t2\t1\t1\t2\t210\t120\t100\t20\t95\tSatz",
            "2\t1\t3\t0\t0\t0\t100\t400\t900\t200\t-1\t",
            "5\t1\t3\t1\t1\t1\t100\t400\t100\t20\t20\t   ",
        ])

        with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout=tsv)):
            blocks = main.ocr_page_blocks(Path("page.png"), "deu")

        self.assertEqual([block["text"] for block in blocks], ["Überschrift", "Erster Satz"])
        self.assertEqual(blocks[1]["box"], (100, 120, 1000, 320))

    def test_ocr_page_blocks_survives_a_failed_call(self):
        # No layout means the caller reads the whole page, which is the old behaviour.
        with patch.object(main.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(main.ocr_page_blocks(Path("page.png"), "deu"), [])

    def test_ocr_block_scripts_only_trusts_blocks_with_enough_text(self):
        # OSD answers whatever it likes on a short block: a 17-character Japanese heading came
        # back "Arabic". Only blocks past the threshold are asked at all.
        blocks = [
            {"box": (0, 0, 500, 300), "text": "x" * main.OCR_BLOCK_MIN_CHARS},
            {"box": (0, 400, 500, 440), "text": "kurz"},
        ]
        with patch.object(main, "render_pdf_region", return_value=Path("crop.png")):
            with patch.object(main, "ocr_page_script", return_value="Devanagari") as osd:
                scripts = main.ocr_block_scripts(b"%PDF", 1, blocks, "Latin", "/tmp")

        self.assertEqual(scripts, ["Devanagari", "Latin"])
        self.assertEqual(osd.call_count, 1)

    def test_ocr_block_scripts_ignores_scripts_no_language_exists_for(self):
        # A Chinese page had a block come back "Korean". ocr_probe_languages can only answer
        # English for that, and reading Han as English returns nothing at all.
        blocks = [{"box": (0, 0, 500, 300), "text": "x" * main.OCR_BLOCK_MIN_CHARS}]
        with patch.object(main, "render_pdf_region", return_value=Path("crop.png")):
            for reported in ("Korean", "HanT", ""):
                with self.subTest(script=reported):
                    with patch.object(main, "ocr_page_script", return_value=reported):
                        self.assertEqual(
                            main.ocr_block_scripts(b"%PDF", 1, blocks, "Han", "/tmp"), ["Han"]
                        )

    def test_scripts_are_compatible_covers_the_han_variants(self):
        self.assertTrue(main.scripts_are_compatible("HanS", "Han"))
        self.assertTrue(main.scripts_are_compatible("HanT", "HanS"))
        # Japanese is written with Han characters, so those two never contradict each other.
        self.assertTrue(main.scripts_are_compatible("Han", "Japanese"))
        self.assertFalse(main.scripts_are_compatible("Han", "Latin"))
        self.assertFalse(main.scripts_are_compatible("Devanagari", "Latin"))

    def test_ocr_pdf_page_reads_a_mixed_page_block_by_block(self):
        # One script per page is what OSD gives, so a page holding two loses the minority one:
        # measured on a half-Hindi half-German scan, the Hindi came back as invented Latin.
        blocks = [
            {"box": (0, 0, 500, 300), "text": "d" * main.OCR_BLOCK_MIN_CHARS},
            {"box": (0, 400, 500, 700), "text": "l" * main.OCR_BLOCK_MIN_CHARS},
        ]
        reads = []

        def fake_read(image_path, script):
            reads.append(script)
            return f"text in {script}"

        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "render_pdf_page", return_value=Path("page.png")):
                with patch.object(main, "render_pdf_region", return_value=Path("crop.png")):
                    with patch.object(main, "ocr_page_script", return_value="Latin"):
                        with patch.object(main, "ocr_page_blocks", return_value=blocks):
                            with patch.object(main, "ocr_block_scripts",
                                              return_value=["Devanagari", "Latin"]):
                                with patch.object(main, "ocr_read_with_detection", fake_read):
                                    text = main.ocr_pdf_page(b"%PDF", 1)

        self.assertEqual(reads, ["Devanagari", "Latin"])
        self.assertEqual(text, "text in Devanagari\n\ntext in Latin")

    def test_ocr_pdf_page_reads_a_single_script_page_in_one_go(self):
        # Splitting a page only to read every block in the same language would cost several OCR
        # runs and throw away the layout analysis' view of the whole page.
        blocks = [{"box": (0, 0, 500, 300), "text": "x" * main.OCR_BLOCK_MIN_CHARS}] * 3

        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "render_pdf_page", return_value=Path("page.png")):
                with patch.object(main, "ocr_page_script", return_value="Latin"):
                    with patch.object(main, "ocr_page_blocks", return_value=blocks):
                        with patch.object(main, "ocr_block_scripts", return_value=["Latin"] * 3):
                            with patch.object(main, "ocr_read_with_detection",
                                              return_value="whole page") as read:
                                text = main.ocr_pdf_page(b"%PDF", 1)

        self.assertEqual(text, "whole page")
        self.assertEqual(read.call_args.args, (Path("page.png"), "Latin"))

    def test_ocr_probe_languages_stay_within_the_detected_script(self):
        installed = ("ara", "bul", "deu", "eng", "fra", "heb", "rus", "spa", "ukr")
        with patch.object(main, "installed_ocr_languages", return_value=installed):
            # Latin: English is in there, and never more than three.
            latin = main.ocr_probe_languages("Latin").split("+")
            self.assertIn("eng", latin)
            self.assertLessEqual(len(latin), main.OCR_PROBE_LIMIT)
            # Cyrillic: no English, it does not occur in that script.
            self.assertEqual(main.ocr_probe_languages("Cyrillic"), "rus+ukr+bul")
            self.assertEqual(main.ocr_probe_languages("Hebrew"), "heb")
            self.assertEqual(main.ocr_probe_languages(""), "eng")
            self.assertEqual(main.ocr_probe_languages("Klingon"), "eng")

    def test_ocr_probe_keeps_english_out_when_a_package_is_missing(self):
        # ukr has no package here. Falling back to eng for it would put a Latin language into
        # a Cyrillic probe, which is exactly what the probe is meant to avoid.
        with patch.object(main, "installed_ocr_languages", return_value=("bul", "eng", "rus")):
            self.assertEqual(main.ocr_probe_languages("Cyrillic"), "rus+bul")

    def test_installed_ocr_languages_drops_only_the_header(self):
        listing = 'List of available languages in "/usr/share/tessdata/" (3):\nara\ndeu\neng\n'
        with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout=listing)):
            main.installed_ocr_languages.cache_clear()
            self.assertEqual(main.installed_ocr_languages(), ("ara", "deu", "eng"))
        main.installed_ocr_languages.cache_clear()

    def test_ocr_page_script_reads_the_osd_report(self):
        report = "Page number: 0\nOrientation in degrees: 0\nScript: Cyrillic\nScript confidence: 3.4\n"
        with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout=report)):
            self.assertEqual(main.ocr_page_script(Path("page.png")), "Cyrillic")

    def test_dominant_text_script_names_the_script(self):
        self.assertEqual(main.dominant_text_script("Hello world"), "Latin")
        self.assertEqual(main.dominant_text_script("הרפובליקה הפדרלית"), "Hebrew")
        self.assertEqual(main.dominant_text_script("Президентские выборы"), "Cyrillic")
        self.assertEqual(main.dominant_text_script("शिक्षा मंत्रालय"), "Devanagari")
        self.assertEqual(main.dominant_text_script("12345 -,."), "")

    def test_text_layer_is_distrusted_when_it_contradicts_the_page(self):
        # A Hebrew page whose font maps to Latin: the text layer claims Latin, the page shows
        # Hebrew. Nothing in the file flags this, the fonts do carry a ToUnicode table.
        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "render_pdf_page", return_value=Path("page.png")):
                with patch.object(main, "ocr_page_script", return_value="Hebrew"):
                    self.assertFalse(main.text_layer_is_trustworthy(b"%PDF", 1, "hinmrg tilrdph"))
                with patch.object(main, "ocr_page_script", return_value="Latin"):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "hinmrg tilrdph"))
                # Japanese is written with Han characters, so those two never contradict.
                with patch.object(main, "ocr_page_script", return_value="Japanese"):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "作成日 東京都"))
                # OSD silent, or a page without letters: nothing to contradict.
                with patch.object(main, "ocr_page_script", return_value=""):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "hinmrg tilrdph"))
                with patch.object(main, "ocr_page_script", return_value="Hebrew"):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "12345"))

    def test_pdf_extraction_falls_back_to_ocr_for_a_lying_text_layer(self):
        document = FakeDocument([FakePage("hinmrg lß tilrdph hqilbuprh und mehr text"),
                                 FakePage("hinmrg lß tilrdph hqilbuprh und mehr text")])

        with patch.object(main.pymupdf, "open", return_value=document):
            with patch.object(main, "text_layer_is_trustworthy", return_value=False) as checked:
                with patch.object(main, "ocr_pdf_page", return_value="הרפובליקה") as mocked_ocr:
                    markdown = main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertIn("הרפובליקה", markdown)
        self.assertEqual(mocked_ocr.call_count, 2)
        # Asked once for the document, not once per page.
        self.assertEqual(checked.call_count, 1)

    def test_ocr_probe_reads_traditional_chinese_with_its_own_model(self):
        installed = ("chi_sim", "chi_tra", "deu", "eng", "fra")
        with patch.object(main, "installed_ocr_languages", return_value=installed):
            self.assertEqual(main.ocr_probe_languages("HanT"), "chi_tra+chi_sim")
            self.assertEqual(main.ocr_probe_languages("Han"), "chi_sim")
        # Without the package it must not appear, an unknown -l aborts tesseract.
        with patch.object(main, "installed_ocr_languages", return_value=("chi_sim", "eng")):
            self.assertEqual(main.ocr_probe_languages("HanT"), "chi_sim")

    def test_ocr_page_script_lowers_the_osd_character_threshold(self):
        # Without this, a page holding one short line gets no answer at all.
        report = "Script: Japanese\nScript confidence: 1.2\n"
        with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout=report)) as run:
            main.ocr_page_script(Path("page.png"))

        self.assertIn("min_characters_to_try=10", run.call_args.args[0])

    def test_ocr_page_script_stays_quiet_when_osd_fails(self):
        # No osd data or too little text: the job must go on, not die.
        with patch.object(main.subprocess, "run", side_effect=OSError("no osd")):
            self.assertEqual(main.ocr_page_script(Path("page.png")), "")

    def test_auto_detect_reads_a_scan_twice(self):
        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "installed_ocr_languages", return_value=("deu", "eng", "fra")):
                with patch.object(main, "ocr_page_script", return_value="Latin"):
                    with patch.object(main, "run_tesseract", side_effect=["Der Vertrag wurde geprueft", "Der Vertrag wurde geprüft"]) as reads:
                        with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout="")):
                            text = main.ocr_pdf_page(b"%PDF", 1)

        self.assertEqual(text, "Der Vertrag wurde geprüft")
        self.assertEqual([call.args[1] for call in reads.call_args_list], ["eng+deu+fra", "deu"])

    def test_auto_detect_rechecks_the_language_on_the_clean_text(self):
        # The probe mangles diacritics, so langdetect can land on a close relative. The clean
        # text from that read settles it.
        detections = iter(["slk_Latn", "ces_Latn", "ces_Latn"])

        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "installed_ocr_languages", return_value=("ces", "deu", "eng", "fra", "slk")):
                with patch.object(main, "ocr_page_script", return_value="Latin"):
                    with patch.object(main, "detect_source_language", side_effect=lambda _text: next(detections)):
                        with patch.object(main, "run_tesseract", side_effect=["probe", "slovak read", "czech read"]) as reads:
                            with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout="")):
                                text = main.ocr_pdf_page(b"%PDF", 1)

        self.assertEqual(text, "czech read")
        self.assertEqual([call.args[1] for call in reads.call_args_list], ["eng+deu+fra", "slk", "ces"])

    def test_auto_detect_skips_the_second_read_when_the_probe_was_one_language(self):
        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "installed_ocr_languages", return_value=("eng", "heb")):
                with patch.object(main, "ocr_page_script", return_value="Hebrew"):
                    with patch.object(main, "run_tesseract", return_value="טקסט") as reads:
                        with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout="")):
                            main.ocr_pdf_page(b"%PDF", 1)

        self.assertEqual(reads.call_count, 1)

    def test_pdf_extraction_reuses_the_language_found_on_the_first_scanned_page(self):
        document = FakeDocument([FakePage(""), FakePage("")])

        with patch.object(main.pymupdf, "open", return_value=document):
            with patch.object(main, "detect_source_language", return_value="fra_Latn"):
                with patch.object(main, "ocr_pdf_page", return_value="texte") as mocked_ocr:
                    main.extract_pdf_markdown_from_bytes(b"%PDF")

        self.assertEqual([call.args[2] for call in mocked_ocr.call_args_list], [main.AUTO_SOURCE, "fra_Latn"])

    def test_pdf_extraction_passes_the_source_language_to_ocr(self):
        document = FakeDocument([FakePage("")])

        with patch.object(main.pymupdf, "open", return_value=document):
            with patch.object(main, "ocr_pdf_page", return_value="OCR text") as mocked_ocr:
                main.extract_pdf_markdown_from_bytes(b"%PDF", source="fra_Latn")

        mocked_ocr.assert_called_once_with(b"%PDF", 1, "fra_Latn")

    def test_create_text_pdf_returns_pdf_document(self):
        content = main.create_text_pdf("Translated text\n\nSecond paragraph")

        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(pdf_page_count(content), 1)
        self.assertIn("Translated text", pdf_text(content))

    def test_create_text_pdf_preserves_markdown_page_breaks(self):
        content = main.create_text_pdf("# Page 1\n\nFirst\n\n# Page 2\n\nSecond")
        text = pdf_text(content)

        self.assertEqual(pdf_page_count(content), 2)
        self.assertIn("Page 1", text)
        self.assertIn("Page 2", text)

    def test_create_text_pdf_formats_markdown_headings(self):
        content = main.create_text_pdf("# Title\n\nBody")

        title = [span for span in pdf_spans(content) if "Title" in span["text"]]
        self.assertEqual(title[0]["size"], main.PDF_HEADING_FONT_SIZE)
        self.assertIn("Title", pdf_text(content))

    def test_pdf_sections_returns_none_without_page_markers(self):
        # re.split returns the whole string as a single part when its pattern never matches;
        # without this check that part's first line was misread as a fake page-number heading
        # for any translation not sourced from a PDF (plain text, DOCX, ...).
        self.assertEqual(main.pdf_sections("Just a single line of translated text."), [])
        self.assertEqual(main.pdf_sections("First line\n\nSecond paragraph"), [])

    def test_markdown_page_sections_keeps_plain_text_as_one_section_without_heading(self):
        sections = main.markdown_page_sections("Hello world, this is the whole translation.")

        self.assertEqual(sections, [{"page_number": "", "text": "Hello world, this is the whole translation."}])

    def test_create_text_pdf_embeds_font_for_non_latin_text(self):
        content = main.create_text_pdf("Privet mir: Привет")

        self.assertIn(b"/FontFile2", content)
        # Round-trips through the ToUnicode CMap, so the text stays selectable.
        self.assertIn("Привет", pdf_text(content))

    def test_detect_pdf_script_identifies_known_scripts(self):
        self.assertEqual(main.detect_pdf_script("Hello world"), "")
        self.assertEqual(main.detect_pdf_script("你好"), "cjk")
        self.assertEqual(main.detect_pdf_script("こんにちは"), "cjk")
        self.assertEqual(main.detect_pdf_script("مرحبا"), "arabic")
        self.assertEqual(main.detect_pdf_script("नमस्ते"), "devanagari")
        self.assertEqual(main.detect_pdf_script("שלום"), "hebrew")

    def test_create_text_pdf_embeds_script_specific_font_for_cjk_text(self):
        content = main.create_text_pdf("こんにちは世界")

        self.assertIn(b"/FontFile2", content)
        self.assertIn("こんにちは世界", pdf_text(content))
        # Glyph id 0 is .notdef, the empty box a font without the script leaves behind.
        page = pymupdf.open(stream=content, filetype="pdf")[0]
        glyph_ids = [item[1] for span in page.get_texttrace() for item in span["chars"]]
        self.assertNotIn(0, glyph_ids)

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
            result = main.export_original_history_content(
                "pdf", b"source pdf", "text", {"layout": "true", "page_range": "2-5"}
            )

        overlay_mock.assert_called_once_with(b"source pdf", "text", "2-5")
        self.assertEqual(result, b"overlay pdf")

    def test_pdf_page_runs_maps_baselines_back_into_pdf_user_space(self):
        # MuPDF counts y downwards from the top of the page, the overlay is drawn in PDF user
        # space. Getting this flip wrong mirrors every cover box to the other end of the page.
        runs = main.pdf_page_runs(FakeMuPdfPage([mupdf_span("Visible", 90.0, 100.0)]))

        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0]["x"], 90.0)
        self.assertAlmostEqual(runs[0]["y"], 700.0)  # page height 800 - 100

    def test_pdf_page_runs_collapses_synthetic_bold_double_strokes(self):
        # Fake bold draws each glyph twice at the identical position, sometimes with a trailing
        # space on only one of the two copies (e.g. "r" then "r ").
        page = FakeMuPdfPage([
            mupdf_span("P", 200.0, 130.0),
            mupdf_span("P", 200.0, 130.0),
            mupdf_span("r ", 316.0, 130.0),
            mupdf_span("r ", 316.0, 130.0),
        ])

        self.assertEqual([run["text"] for run in main.pdf_page_runs(page)], ["P", "r "])

    def test_pdf_page_runs_skips_hidden_text_anchored_at_origin(self):
        page = FakeMuPdfPage([
            mupdf_span("hidden duplicate page text", 0.0, 800.0),  # user-space (0, 0)
            mupdf_span("Visible", 90.0, 100.0),
        ])

        self.assertEqual([run["text"] for run in main.pdf_page_runs(page)], ["Visible"])

    def test_pdf_page_runs_reads_bold_from_the_mupdf_span_flag(self):
        page = FakeMuPdfPage([
            mupdf_span("Heading", 50.0, 100.0, bold=True),
            mupdf_span("Body", 50.0, 120.0),
        ])

        self.assertEqual([run["bold"] for run in main.pdf_page_runs(page)], [True, False])

    def test_pdf_font_is_serif_reads_the_family_name(self):
        for name in ("TimesNewRomanPSMT", "Georgia", "ABCDEF+DejaVuSerif", "Garamond", "BookAntiqua"):
            with self.subTest(font=name):
                self.assertTrue(main.pdf_font_is_serif(name))
        # "sans" wins over any marker: NotoSansSerif-style names are sans faces.
        for name in ("Arial", "Roboto", "NotoSans", "DejaVuSans", "Helvetica", ""):
            with self.subTest(font=name):
                self.assertFalse(main.pdf_font_is_serif(name))

    def test_pdf_page_runs_reads_the_serif_character_from_the_font_name(self):
        # MuPDF's own "serifed" span flag comes from the font descriptor, which Roboto (a sans
        # face) sets - so the name has to decide, not the flag.
        page = FakeMuPdfPage([
            mupdf_span("Vertrag", 50.0, 100.0, font="TimesNewRomanPSMT"),
            mupdf_span("Heading", 50.0, 120.0, font="Roboto"),
        ])

        self.assertEqual([run["serif"] for run in main.pdf_page_runs(page)], [True, False])

    def test_reflow_paragraph_draws_a_serif_paragraph_in_the_serif_font(self):
        for bold, serif, expected in ((False, False, "F1"), (True, False, "F2"),
                                      (False, True, "F3"), (True, True, "F4")):
            with self.subTest(bold=bold, serif=serif):
                paragraph = {"lines": [{
                    "text": "Text", "x": 50.0, "y": 700.0, "right": 200.0,
                    "size": 11.0, "bold": bold, "serif": serif,
                }]}
                placed = main.reflow_paragraph(paragraph, "Uebersetzung")
                self.assertEqual(placed[0]["font"], expected)

    def test_pdf_page_runs_skips_rotated_and_vertical_lines(self):
        # The overlay is stamped horizontally, so sideways text has to keep its original.
        page = FakeMuPdfPage([
            mupdf_span("sideways", 50.0, 100.0, direction=(0.0, 1.0)),
            mupdf_span("upside down", 50.0, 200.0, direction=(-1.0, 0.0)),
            mupdf_span("Horizontal", 50.0, 300.0),
        ])

        self.assertEqual([run["text"] for run in main.pdf_page_runs(page)], ["Horizontal"])

    def test_group_pdf_lines_merges_runs_on_the_same_baseline(self):
        runs = [
            {"text": "Hello", "x": 50.0, "y": 700.0, "size": 11.0},
            {"text": "world", "x": 90.0, "y": 700.2, "size": 11.0},
            {"text": "Second", "x": 50.0, "y": 686.0, "size": 11.0},
        ]

        lines = main.group_pdf_lines(runs)

        self.assertEqual([line["text"] for line in lines], ["Hello world", "Second"])
        self.assertGreater(lines[0]["right"], 90.0)

    def test_group_pdf_lines_takes_the_furthest_right_edge_of_overlapping_runs(self):
        # Runs reported at the same position used to be treated as continuing from each other and
        # their widths added up, which is how pypdf reported a text block. MuPDF gives every span
        # its own real x, so adding widths only inflates the line's right edge - and that edge is
        # what the cell-gap check below measures against.
        runs = [
            {"text": "Hello", "x": 50.0, "y": 700.0, "size": 11.0, "width": 30.0},
            {"text": "world", "x": 50.0, "y": 700.0, "size": 11.0, "width": 40.0},
        ]

        lines = main.group_pdf_lines(runs)

        self.assertEqual(lines[0]["text"], "Helloworld")
        self.assertAlmostEqual(lines[0]["right"], 90.0)

    def test_group_pdf_lines_splits_table_cells_on_the_same_baseline(self):
        # Three cells of a table row share one baseline. Merged into a single line, the row's
        # translation gets reflowed across the whole table width and printed over the columns
        # next to it - which is what "Typ Minimum empfohlen" overlapping itself looked like.
        runs = [
            {"text": "RAM", "x": 50.0, "y": 700.0, "size": 9.0, "width": 25.0},
            {"text": "8 GB", "x": 200.0, "y": 700.0, "size": 9.0, "width": 25.0},
            {"text": "16 GB", "x": 400.0, "y": 700.0, "size": 9.0, "width": 30.0},
        ]

        lines = main.group_pdf_lines(runs)

        self.assertEqual([line["text"] for line in lines], ["RAM", "8 GB", "16 GB"])
        self.assertEqual([line["x"] for line in lines], [50.0, 200.0, 400.0])

    def test_group_pdf_lines_keeps_ordinary_word_spacing_in_one_line(self):
        # The mirror case: normal gaps between words, and even a wide tab stop, must not be read
        # as a cell boundary or every justified line falls apart into fragments.
        runs = [
            {"text": "Ein", "x": 50.0, "y": 700.0, "size": 11.0, "width": 18.0},
            {"text": "kurzer", "x": 71.0, "y": 700.0, "size": 11.0, "width": 30.0},
            {"text": "Satz", "x": 110.0, "y": 700.0, "size": 11.0, "width": 22.0},
        ]

        lines = main.group_pdf_lines(runs)

        self.assertEqual([line["text"] for line in lines], ["Ein kurzer Satz"])

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

        placed = main.reflow_paragraph(paragraph, long_text)

        # Shrunk, but never below the floor, and never dropping lines to make it fit.
        self.assertLess(placed[0]["size"], 11.0)
        self.assertGreaterEqual(placed[0]["size"], 11.0 * main.PDF_LAYOUT_MIN_SCALE)
        self.assertGreater(len(placed), 2)
        self.assertEqual(" ".join(line["text"] for line in placed).split(), long_text.split())

    def test_reflow_paragraph_tightens_slightly_rather_than_adding_a_line(self):
        # The substitute font runs wider than the document's own, so text that filled one line
        # in the original spills into two. A few percent smaller is better than an extra line.
        text = "Gerade eben zu breit"
        # Derived from the measured width, not hard-coded, so the test does not depend on which
        # font the machine running it happens to embed: 3% too narrow needs ~3% of tightening.
        width = main.pdf_measure_text(text, 11.0) * 0.97
        paragraph = {"lines": [
            {"text": "x", "x": 50.0, "y": 700.0, "right": 50.0 + width, "size": 11.0},
        ]}
        # Confirm the premise: at full size this really does need a second line.
        self.assertGreater(len(main.wrap_text_to_width(text, width, 11.0)), 1)

        placed = main.reflow_paragraph(paragraph, text)

        self.assertEqual(len(placed), 1)
        self.assertLess(placed[0]["size"], 11.0)
        self.assertGreaterEqual(placed[0]["size"], 11.0 * main.PDF_LAYOUT_TIGHTEN_SCALE)

    def test_reflow_paragraph_leaves_text_that_already_fits_at_full_size(self):
        paragraph = {"lines": [
            {"text": "x", "x": 50.0, "y": 700.0, "right": 300.0, "size": 11.0},
        ]}

        placed = main.reflow_paragraph(paragraph, "Kurz")

        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]["size"], 11.0)

    def test_reflow_paragraph_spaces_overflow_lines_at_the_shrunken_size(self):
        # Overflow lines are set at the shrunken size, so spacing them at the original leading
        # pushes them further down than they need to go - into the next paragraph.
        paragraph = {"lines": [
            {"text": "One", "x": 50.0, "y": 700.0, "right": 200.0, "size": 22.0},
            {"text": "two", "x": 50.0, "y": 660.0, "right": 200.0, "size": 22.0},
        ]}

        placed = main.reflow_paragraph(paragraph, "Eine deutlich laengere Uebersetzung " * 4)

        self.assertGreater(len(placed), 2)
        overflow_steps = [a["y"] - b["y"] for a, b in zip(placed[1:], placed[2:])]
        for step in overflow_steps:
            self.assertLessEqual(step, 1.2 * placed[0]["size"] + 0.01)
            self.assertLess(step, 40.0)  # the original leading

    def test_reflow_paragraph_does_not_shrink_when_the_overflow_has_room(self):
        # With nothing below it, a paragraph used to be shrunk to the floor purely for having
        # more lines than the original. Knowing what is below lets it stay readable.
        paragraph = {"lines": [
            {"text": "Short", "x": 50.0, "y": 700.0, "right": 200.0, "size": 11.0},
        ]}
        text = "Eine etwas laengere Uebersetzung die umbrechen muss"

        cramped = main.reflow_paragraph(paragraph, text, floor=690.0)
        roomy = main.reflow_paragraph(paragraph, text, floor=400.0)

        # Room below still means less shrinking. Not asserting an absolute size here: how far
        # this particular text has to shrink depends on the font the machine embeds.
        self.assertGreater(roomy[0]["size"], cramped[0]["size"])
        # Complete either way: shrinking is the fix, dropping words never is.
        for placed in (cramped, roomy):
            self.assertEqual(" ".join(line["text"] for line in placed).split(), text.split())

    def test_split_to_sentences_gives_the_model_one_sentence_at_a_time(self):
        # OPUS-MT gives up part-way through a long multi-sentence input: a real 1853-character
        # paragraph came back 59% translated, three bullet points short, while the same text
        # split into sentences came back whole.
        class WordTokenizer:
            def __call__(self, text):
                return SimpleNamespace(input_ids=text.split())

        text = 'Erster Satz. Zweiter Satz! Dritter? "Vierter." Fuenfter.'
        parts = main.split_to_sentences(WordTokenizer(), text)

        self.assertEqual(len(parts), 5)
        self.assertEqual(parts[0], "Erster Satz. ")
        # Not a single character may be lost or duplicated - a closing quote used to be eaten
        # along with the separator.
        self.assertEqual("".join(parts), text)
        self.assertIn('"Vierter." ', parts)

    def test_split_to_sentences_keeps_a_single_sentence_whole(self):
        class WordTokenizer:
            def __call__(self, text):
                return SimpleNamespace(input_ids=text.split())

        self.assertEqual(
            main.split_to_sentences(WordTokenizer(), "Nur ein Satz ohne Ende"),
            ["Nur ein Satz ohne Ende"],
        )

    def test_split_to_token_limit_keeps_every_part_within_the_window(self):
        # The model truncates its input silently, so anything past the window is simply lost.
        class WordTokenizer:
            """Stands in for the real tokenizer: one token per word."""

            def __call__(self, text):
                return SimpleNamespace(input_ids=text.split())

        text = ". ".join(f"Satz nummer {n} mit etwas Text" for n in range(300)) + "."
        with patch.object(main, "TRANSLATE_TEXT_TOKENS", 50):
            parts = main.split_to_token_limit(WordTokenizer(), text)

        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part.split()), 50)
        # Nothing may be dropped or duplicated on the way.
        self.assertEqual("".join(parts), text)

    def test_split_to_token_limit_leaves_a_short_text_alone(self):
        class WordTokenizer:
            def __call__(self, text):
                return SimpleNamespace(input_ids=text.split())

        self.assertEqual(main.split_to_token_limit(WordTokenizer(), "Kurzer Satz."), ["Kurzer Satz."])

    def test_split_to_token_limit_splits_text_without_any_boundary(self):
        # A single unbroken run (no spaces, no sentence ends) must still terminate, not recurse.
        class CharTokenizer:
            def __call__(self, text):
                return SimpleNamespace(input_ids=list(text))

        with patch.object(main, "TRANSLATE_TEXT_TOKENS", 10):
            parts = main.split_to_token_limit(CharTokenizer(), "x" * 100)

        self.assertEqual("".join(parts), "x" * 100)
        for part in parts:
            self.assertLessEqual(len(part), 10)

    def test_has_translatable_text_rejects_wordless_fragments(self):
        # These are what a line clipped by the page edge leaves behind. Sent to the model they
        # come back as invented text that is then laid out over the page.
        for fragment in ("", " ", ".", ",", ";", "y", "-", "  .  ", "1", "42", "...",
                         "y g", "a b c", "09.08.2026, 05:44"):
            with self.subTest(fragment=fragment):
                self.assertFalse(main.has_translatable_text(fragment))
        # A bare URL is not prose, cannot be wrapped, and comes back rewritten.
        for url in ("https://www.reddit.com/r/mecfs/comments/19525fp/mecfs_recovery",
                    "http://example.com", "www.malighting.com"):
            with self.subTest(url=url):
                self.assertFalse(main.has_translatable_text(url))
        # Roman page numbers clear the two-letter bar but say as little as "4" does; the model
        # answered "iv" with "iv iv iv iv iv" across the bottom of the page.
        for numeral in ("i", "iv", "ix", "xiv", "iv.", "XXVII"):
            with self.subTest(numeral=numeral):
                self.assertFalse(main.has_translatable_text(numeral))
        # Dot leaders are decoration, so a line that is only leaders has nothing to translate.
        self.assertFalse(main.has_translatable_text(". . . . . . . ."))
        for real in ("Ja", "Hello world", "2. Scope", "16 GB", "日本語",
                     "Mehr dazu auf https://example.com nachlesen",
                     "1. Gesichtspflege . . . . . . . . 12"):
            with self.subTest(text=real):
                self.assertTrue(main.has_translatable_text(real))

    def test_clean_source_text_drops_dot_leaders(self):
        # A row of dots reads to the model as an unfinished sentence, and it completes it with
        # European Parliament boilerplate that then gets laid out across the page.
        self.assertEqual(main.clean_source_text("Duschen . . . . . . . . 15 Minuten"),
                         "Duschen 15 Minuten")
        self.assertEqual(main.clean_source_text("Fernsehen........30 Minuten"),
                         "Fernsehen 30 Minuten")
        # An ellipsis is punctuation, not a leader, and stays.
        self.assertEqual(main.clean_source_text("Warte... ich komme"), "Warte... ich komme")

    def test_guard_hallucination_keeps_original_when_output_explodes(self):
        # "Chapter 1 - Development" came back as "ENTWICKLUNG DER ENTWICKLUNG DER ..." over half
        # the page; a page number "-" as "- Nein, nein, nein, ...".
        source = "Kapitel 1 - Entwicklung"
        self.assertEqual(main.guard_hallucination(source, "ENTWICKLUNG DER " * 20), source)
        self.assertEqual(main.guard_hallucination("-", "- Nein, " * 30), "-")
        # A genuine translation, even a much longer one, is kept.
        self.assertEqual(main.guard_hallucination("Yes", "Ja, natürlich"), "Ja, natürlich")
        self.assertEqual(
            main.guard_hallucination("Building energy", "Aufbau der körperlichen Energie"),
            "Aufbau der körperlichen Energie",
        )

    def test_layout_overlay_leaves_wordless_fragments_untouched(self):
        # A fragment keeps its original: it must be neither redacted away nor redrawn, or the
        # page loses a character it could have kept.
        pages = [{
            "number": 1, "width": 400.0, "height": 300.0,
            "paragraphs": [
                {"text": "Hello world", "lines": [
                    {"text": "Hello world", "x": 50.0, "y": 200.0, "right": 150.0, "size": 11.0},
                ]},
                {"text": ",", "lines": [
                    {"text": ",", "x": 50.0, "y": 180.0, "right": 55.0, "size": 11.0},
                ]},
            ],
        }]
        source = main.create_pdf_from_pages([{
            "width": 400, "height": 300, "margin": 40,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [
                {"text": "Hello world", "font": "F1", "size": 11, "line_height": 14, "x": 50, "y": 200},
                {"text": ",", "font": "F1", "size": 11, "line_height": 14, "x": 50, "y": 180},
            ],
        }])

        # The runner stores a fragment's own text in its slot, so positions stay aligned.
        overlay = main.render_pdf_layout_overlay(source, pages, ["Hallo Welt", ","])
        text = pdf_text(overlay)

        self.assertIn("Hallo Welt", text)
        self.assertNotIn("Hello world", text)
        self.assertIn(",", text)

    def test_layout_overlay_keeps_the_original_when_the_reflow_explodes(self):
        # The model answers a heading with several lines of invented text; laid out, that block
        # buries everything below it. The original heading is the better of the two to keep.
        pages = [{
            "number": 1, "width": 400.0, "height": 300.0,
            "paragraphs": [
                {"text": "Chapter one", "lines": [
                    {"text": "Chapter one", "x": 50.0, "y": 200.0, "right": 150.0, "size": 11.0},
                ]},
            ],
        }]
        source = main.create_pdf_from_pages([{
            "width": 400, "height": 300, "margin": 40,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [{"text": "Chapter one", "font": "F1", "size": 11, "line_height": 14,
                       "x": 50, "y": 200}],
        }])

        overlay = main.render_pdf_layout_overlay(source, pages, ["ENTWICKLUNG DER " * 30])
        text = pdf_text(overlay)

        # Neither redacted away nor overdrawn.
        self.assertIn("Chapter one", text)
        self.assertNotIn("ENTWICKLUNG", text)

    def test_is_rtl_text_recognises_the_right_to_left_scripts(self):
        for text in ("מפתח הכוכבים ויער האשליות", "الكتاب", "שלום 2026"):
            with self.subTest(text=text):
                self.assertTrue(main.is_rtl_text(text))
        # Devanagari runs left to right, and a stray Hebrew word in a German line does not turn
        # the line around.
        for text in ("Guten Morgen", "सितारों की कुंजी", "2026-08-11", "",
                     "Das hebräische Wort שלום bedeutet Frieden und Ganzheit"):
            with self.subTest(text=text):
                self.assertFalse(main.is_rtl_text(text))

    def test_pdf_page_runs_reads_right_to_left_characters_in_page_order(self):
        # The order a span is painted in is the producer's business; the x of each character is
        # not. A Hebrew span whose glyphs are drawn out of order still has to come back in the
        # order it stands on the page, so the line can be turned around later.
        visual = "םיבכוכה חתפמ"
        page = FakeMuPdfPage([mupdf_span(visual, 50, 700, draw_order=visual[::-1])])

        runs = main.pdf_page_runs(page)

        self.assertEqual([run["text"] for run in runs], [visual])

    def test_group_pdf_lines_turns_right_to_left_text_into_reading_order(self):
        # Assembled left to right, a Hebrew line arrives back to front. The model needs it the
        # way it is read.
        logical = "שלום עולם"
        page = FakeMuPdfPage([mupdf_span(logical[::-1], 50, 700)])

        lines = main.group_pdf_lines(main.pdf_page_runs(page))

        self.assertEqual([line["text"] for line in lines], [logical])

    def test_visual_to_logical_keeps_digits_and_latin_words_readable(self):
        # Numbers and Latin words inside a Hebrew line already run left to right; reversing the
        # line as a whole would turn "2024" into "4202".
        self.assertEqual(main.visual_to_logical("2024 םילשורי"), "ירושלים 2024")
        # The full stop of an RTL sentence stands at its left end.
        self.assertEqual(main.visual_to_logical(".םולש"), "שלום.")

    def test_visual_to_logical_puts_a_stray_full_stop_back_at_the_end(self):
        # A converter that skips the bidi algorithm leaves the sentence's full stop at the right
        # edge of the line, which for RTL is where the line begins, so it arrives here in front
        # of the text. Measured on a Word document converted by Stirling-PDF.
        self.assertEqual(main.visual_to_logical("םיקיתעה םיפנעה."), "הענפים העתיקים.")
        self.assertEqual(main.visual_to_logical("רעיה תמשנ'."), "נשמת היער'.")
        # A line that genuinely opens with a quotation keeps it.
        self.assertEqual(main.visual_to_logical("םולש'"), "'שלום")

    def test_pdf_page_runs_measures_a_right_to_left_span_from_its_left_edge(self):
        # MuPDF reports the origin of an RTL span where reading starts, its right edge, while
        # everything downstream measures a line from the left. Taken as given, a full stop drawn
        # as its own span sorted behind the text it ends.
        span = mupdf_span("םיקיתעה", 100, 700)
        span["origin"] = (span["bbox"][2], span["origin"][1])

        runs = main.pdf_page_runs(FakeMuPdfPage([span]))

        self.assertAlmostEqual(runs[0]["x"], 100, delta=0.01)

    def test_visual_to_logical_keeps_closing_punctuation_in_its_order(self):
        # Punctuation at the left end of the line has nothing before it to belong to. Taken as a
        # stretch of its own it runs the wrong way and the quote and full stop swap places, which
        # is what a real Hebrew page turned up.
        logical = "נשמת היער'."
        self.assertEqual(main.visual_to_logical(logical[::-1]), logical)

    def test_pdf_page_runs_drops_a_second_copy_drawn_over_the_first(self):
        # Synthetic bold paints the same glyphs twice ("PPoowweerr"), and some generators leave a
        # whole second copy of the page's text behind, offset and often broken. Only one of them
        # is what the reader sees.
        page = FakeMuPdfPage([
            mupdf_span("Kapitel eins", 50, 700),
            mupdf_span("Kapitel eins", 50.3, 700),      # synthetic bold
            mupdf_span("ԿԮԧԴԳԬԹԵ", 51, 699),            # broken duplicate layer, offset
            mupdf_span("Eigener Absatz", 50, 660),
        ])

        runs = main.pdf_page_runs(page)

        self.assertEqual([run["text"] for run in runs], ["Kapitel eins", "Eigener Absatz"])

    def test_pdf_page_runs_drops_a_broken_copy_drawn_over_rtl_text(self):
        # The generator of the Hebrew sample leaves a second, broken-encoded copy over the real
        # text. Only the first one drawn is what the reader sees.
        page = FakeMuPdfPage([
            mupdf_span("םיבכוכה חתפמ", 50, 700),
            mupdf_span("ԿԮԧԴԳԬԹԵ", 50.5, 700),
        ])

        self.assertEqual([run["text"] for run in main.pdf_page_runs(page)], ["םיבכוכה חתפמ"])

    def test_a_hebrew_translation_is_written_right_to_left(self):
        # Placed codepoint by codepoint from the left, a Hebrew line ends up on the page back to
        # front. Reading our own output back is the check: it goes through the same turn-around
        # a source document gets, so the line only comes out again if it was written the right
        # way round.
        text = "שלום עולם"
        self.assertEqual(self.written_and_read_back(text), [fold(text)])

    def test_a_year_inside_a_hebrew_line_keeps_its_own_direction(self):
        # The line turns around, the number and the Latin word inside it do not: written as one
        # reversed string, "2024" would stand on the page as "4202".
        text = "ירושלים 2024 ABC"
        self.assertEqual(self.written_and_read_back(text), [fold(text)])

    def test_an_arabic_vowel_mark_stays_on_the_letter_it_belongs_to(self):
        # A harakat follows its letter and takes up no width of its own, so a line reversed
        # character by character drops it behind the letter before: measured on a real Arabic
        # page, معروفًا came back as معروًفا.
        text = "معروفًا بين سكان البلدة"
        self.assertEqual(self.written_and_read_back(text), [fold(text)])

    def written_and_read_back(self, text):
        """Our own output, read back through the extractor that turns RTL lines around."""
        pdf = main.create_pdf_from_pages([{
            "width": 300, "height": 200, "margin": 20,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [{"text": text, "font": "F1", "size": 14, "line_height": 18,
                       "x": 20, "y": 150}],
        }])

        page = pymupdf.open(stream=pdf, filetype="pdf")[0]
        # The Arabic fallback font's ToUnicode names the presentation form of each letter rather
        # than the letter itself, so both sides are compared folded back to base characters.
        read_back = [fold(line["text"])
                     for line in main.group_pdf_lines(main.pdf_page_runs(page))]
        # Nothing at all means the machine has no font for the script, which is not a failure.
        return read_back or [fold(text)]

    def test_reflow_paragraph_hangs_a_right_to_left_translation_off_the_right_edge(self):
        paragraph = {"lines": [
            {"text": "Key of the stars", "x": 100.0, "y": 700.0, "right": 300.0, "size": 12.0},
        ]}

        placed = main.reflow_paragraph(paragraph, "מפתח הכוכבים")

        width = main.pdf_measure_text(placed[0]["text"], placed[0]["size"])
        self.assertAlmostEqual(placed[0]["x"] + width, 300.0, delta=1)

    def test_reflow_paragraph_carries_the_original_colour(self):
        # A title set in white on a dark cover image was redrawn in the default black.
        white = 0xFFFFFF
        paragraph = {"lines": [
            {"text": "Power up", "x": 50.0, "y": 700.0, "right": 300.0, "size": 30.0, "color": white},
        ]}

        placed = main.reflow_paragraph(paragraph, "Einschalten")

        self.assertEqual(placed[0]["color"], white)

    def test_shaped_pages_come_out_as_a_readable_pdf(self):
        pdf = main.create_pdf_from_pages([{
            "width": 300, "height": 200, "margin": 20,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [{"text": "सितारों की कुंजी", "font": "F1", "size": 12,
                       "line_height": 14, "x": 20, "y": 150}],
        }])

        self.assertTrue(pdf.startswith(b"%PDF"))
        page = pymupdf.open(stream=pdf, filetype="pdf")[0]
        self.assertEqual(len(pymupdf.open(stream=pdf, filetype="pdf")), 1)
        # Drawn near the baseline it was given, counted from the bottom of the page.
        drawn = [span for block in page.get_text("dict")["blocks"]
                 for line in block.get("lines", []) for span in line["spans"]]
        if drawn:  # no Devanagari font on the machine means nothing to place, not a failure
            self.assertAlmostEqual(200 - drawn[0]["origin"][1], 150, delta=2)
            # Every character reached a glyph of its own. Glyph id 0 is .notdef, the empty box
            # that a missing font or an unshaped cluster leaves behind.
            glyph_ids = [item[1] for span in page.get_texttrace() for item in span["chars"]]
            self.assertNotIn(0, glyph_ids)

    def test_a_coloured_line_does_not_tint_the_lines_after_it(self):
        # The PDF fill colour outlives the text object that set it: a black line drawn after
        # a white one and left to the default would inherit the white and vanish.
        page = {
            "width": 300, "height": 200, "margin": 20,
            "source_page": "", "continuation": False, "footer": True,
            "lines": [
                {"text": "weiss", "font": "F1", "size": 10, "line_height": 12, "color": 0xFFFFFF},
                {"text": "schwarz", "font": "F1", "size": 10, "line_height": 12},
            ],
        }

        spans = pdf_spans(main.create_pdf_from_pages([page]))

        colours = {span["text"].strip(): span["color"] for span in spans}
        self.assertEqual(colours["weiss"], 0xFFFFFF)
        # The black line and the page footer stay black.
        self.assertEqual(colours["schwarz"], 0x000000)
        self.assertEqual(colours["1"], 0x000000)

    def test_group_pdf_lines_takes_the_colour_most_of_the_line_is_set_in(self):
        runs = [
            {"text": "weiss", "x": 50.0, "y": 700.0, "size": 10.0, "width": 30.0, "color": 0xFFFFFF},
            {"text": "auch weiss", "x": 82.0, "y": 700.0, "size": 10.0, "width": 60.0, "color": 0xFFFFFF},
            {"text": "rot", "x": 144.0, "y": 700.0, "size": 10.0, "width": 20.0, "color": 0xFF0000},
        ]

        lines = main.group_pdf_lines(runs)

        self.assertEqual(lines[0]["color"], 0xFFFFFF)

    def test_paragraph_width_limit_uses_the_space_beside_the_paragraph(self):
        # A heading's own ink ends where the original wording ended, which says nothing about
        # how much room it had. Measuring against that made almost every paragraph wrap early.
        heading = {"lines": [{"text": "Heading", "x": 50.0, "y": 700.0, "right": 150.0, "size": 14.0}]}
        below = {"lines": [{"text": "body", "x": 50.0, "y": 680.0, "right": 500.0, "size": 11.0}]}

        limit = main.paragraph_width_limit(heading, [heading, below], 595.0)

        self.assertEqual(limit, 595.0 - main.PDF_LAYOUT_EDGE_MARGIN)

    def test_paragraph_width_limit_stops_at_the_next_column(self):
        left = {"lines": [{"text": "cell", "x": 50.0, "y": 700.0, "right": 150.0, "size": 10.0}]}
        right = {"lines": [{"text": "other", "x": 300.0, "y": 700.0, "right": 400.0, "size": 10.0}]}

        self.assertEqual(main.paragraph_width_limit(left, [left, right], 595.0), 298.0)

    def test_paragraph_width_limit_never_reports_less_than_the_text_itself(self):
        # A paragraph overlapping something to its right must still get its own width, not a
        # negative one.
        wide = {"lines": [{"text": "wide", "x": 50.0, "y": 700.0, "right": 400.0, "size": 10.0}]}
        overlapping = {"lines": [{"text": "x", "x": 60.0, "y": 700.0, "right": 70.0, "size": 10.0}]}

        self.assertEqual(main.paragraph_width_limit(wide, [wide, overlapping], 595.0), 400.0)

    def test_paragraph_floor_ignores_paragraphs_beside_the_column(self):
        target = {"lines": [{"text": "cell", "x": 50.0, "y": 700.0, "right": 150.0, "size": 10.0}]}
        below = {"lines": [{"text": "next row", "x": 50.0, "y": 680.0, "right": 150.0, "size": 10.0}]}
        beside = {"lines": [{"text": "same row", "x": 300.0, "y": 700.0, "right": 400.0, "size": 10.0}]}
        # A neighbouring column further down must not pull the floor up either.
        beside_below = {"lines": [{"text": "other column", "x": 300.0, "y": 690.0, "right": 400.0, "size": 10.0}]}

        floor = main.paragraph_floor(target, [target, below, beside, beside_below])

        self.assertEqual(floor, 680.0)
        # Nothing below in this column: the paragraph may run down to the page edge rather than
        # being shrunk to fit its own lines.
        self.assertEqual(main.paragraph_floor(target, [target, beside, beside_below]),
                         main.PDF_LAYOUT_EDGE_MARGIN)

    def test_group_pdf_lines_marks_a_line_bold_by_majority_of_its_text(self):
        mostly_bold = main.group_pdf_lines([
            {"text": "Important heading", "x": 50.0, "y": 700.0, "size": 11.0, "width": 90.0, "bold": True},
            {"text": "x", "x": 141.0, "y": 700.0, "size": 11.0, "width": 5.0, "bold": False},
        ])
        self.assertTrue(mostly_bold[0]["bold"])

        mostly_regular = main.group_pdf_lines([
            {"text": "Note:", "x": 50.0, "y": 680.0, "size": 11.0, "width": 25.0, "bold": True},
            {"text": "a much longer regular remark follows", "x": 80.0, "y": 680.0, "size": 11.0, "width": 150.0, "bold": False},
        ])
        self.assertFalse(mostly_regular[0]["bold"])

    def test_reflow_paragraph_draws_a_bold_paragraph_in_the_bold_font(self):
        bold_paragraph = {"lines": [
            {"text": "Heading", "x": 50.0, "y": 700.0, "right": 200.0, "size": 14.0, "bold": True},
        ]}
        placed = main.reflow_paragraph(bold_paragraph, "Ueberschrift")
        self.assertEqual([line["font"] for line in placed], ["F2"])

        regular_paragraph = {"lines": [
            {"text": "Body", "x": 50.0, "y": 680.0, "right": 200.0, "size": 11.0, "bold": False},
        ]}
        placed = main.reflow_paragraph(regular_paragraph, "Fliesstext")
        self.assertEqual([line["font"] for line in placed], ["F1"])

    def test_wrap_text_to_width_keeps_every_word(self):
        text = "eins zwei drei vier fuenf sechs sieben acht"

        wrapped = main.wrap_text_to_width(text, 60.0, 11.0)

        self.assertGreater(len(wrapped), 1)
        self.assertEqual(" ".join(wrapped).split(), text.split())

    def test_wrap_text_to_width_breaks_cjk_text_without_spaces(self):
        # Japanese/Chinese have no spaces between words, so word-splitting would treat the
        # whole string as one unbreakable unit and it would run off the page edge.
        text = "こんにちは" * 40

        wrapped = main.wrap_text_to_width(text, 100.0, 11.0)

        self.assertGreater(len(wrapped), 1)
        self.assertEqual("".join(wrapped), text)
        for line in wrapped:
            self.assertLessEqual(main.pdf_measure_text(line, 11.0), 100.0)

    def test_export_pdf_layout_with_translated_text_respects_page_range(self):
        # A job translated with page_range="2" only produced translations for page 2's
        # paragraphs; re-export must extract page 2 only too, or the translation (meant for
        # page 2) gets matched against page 1's paragraphs instead. Only the selected pages
        # are exported, so an untranslated page 1 does not make the result look unconverted.
        source = main.create_pdf_from_pages([
            {
                "width": 400, "height": 300, "margin": 40,
                "source_page": "", "continuation": False, "footer": False,
                "lines": [{"text": "First page original", "font": "F1", "size": 11, "line_height": 14}],
            },
            {
                "width": 400, "height": 300, "margin": 40,
                "source_page": "", "continuation": False, "footer": False,
                "lines": [{"text": "Second page original", "font": "F1", "size": 11, "line_height": 14}],
            },
        ])

        overlay = main.export_pdf_layout_with_translated_text(source, "Second page translated", "2")

        # The white cover only hides the original text visually; the underlying text layer is
        # still extractable, so assert on presence rather than absence.
        pages_text = [page.extract_text() or "" for page in main.PdfReader(BytesIO(overlay)).pages]
        self.assertEqual(len(pages_text), 1)
        self.assertIn("Second page translated", pages_text[0])
        self.assertNotIn("First page original", pages_text[0])

    def test_create_pdf_from_pages_embeds_a_serif_face_for_f3(self):
        # The whole point of the serif face: it has to reach the actual PDF as a different font,
        # not just as a different resource name pointing at DejaVu Sans again.
        source = main.create_pdf_from_pages([{
            "width": 400, "height": 300, "margin": 40,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [
                {"text": "Sans line", "font": "F1", "size": 11, "line_height": 14, "x": 50, "y": 200},
                {"text": "Serif line", "font": "F3", "size": 11, "line_height": 14, "x": 50, "y": 180},
            ],
        }])

        fonts = {span["text"].strip(): span["font"] for span in pdf_spans(source)}
        self.assertNotEqual(fonts["Sans line"], fonts["Serif line"])
        self.assertRegex(fonts["Serif line"].lower(), r"serif|times")

    def test_layout_overlay_draws_one_size_for_the_whole_page(self):
        # Each paragraph used to shrink itself just far enough for its own translation, which put
        # body text at 0.7 next to body text at 1.0 in the same column.
        def line(text, y):
            return {"text": text, "font": "F1", "size": 11, "line_height": 14, "x": 60, "y": y}

        source = main.create_pdf_from_pages([{
            "width": 400, "height": 300, "margin": 40,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [line("The first paragraph stays as short as it was", 250),
                      line("and needs no room beyond its own two lines.", 236),
                      line("The second paragraph grows a good deal", 180),
                      line("longer once it has been translated.", 166)],
        }])
        pages = main.extract_pdf_layout(source)
        self.assertEqual(len(pages[0]["paragraphs"]), 2)

        overlay = main.render_pdf_layout_overlay(source, pages, [
            "Der erste Absatz bleibt so kurz wie er war und braucht keinen Platz.",
            "Der zweite Absatz wird nach der Uebersetzung deutlich laenger, so viel "
            "laenger dass er in seine urspruenglichen zwei Zeilen nur noch kleiner passt.",
        ])

        sizes = {round(span["size"], 2) for span in pdf_spans(overlay)}
        self.assertEqual(len(sizes), 1, f"page drawn in {sizes}")
        self.assertLess(sizes.pop(), 11)

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
        # Painting a box over the original left it copy/pasteable and findable with Ctrl+F, so
        # a "translated" document still handed the reader the source wording.
        self.assertNotIn("Hello", text)

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
                    with patch.object(main, "translate_batch", return_value=["Hallo Welt"]):
                        main.run_pdf_layout_translate_job(job_id, source, "eng_Latn", "deu_Latn", "input.pdf")

                    job = main.get_job(job_id)
                    history = main.history_item(job["history_id"])
                    exported = main.export_original_history_content(
                        "pdf", source, job["result"], history["source_meta"]
                    )

            self.assertEqual(job["status"], "complete")
            self.assertEqual(job["result"], "Hallo Welt")
            self.assertEqual(history["source_extension"], "pdf")
            self.assertEqual(history["source_meta"], {"layout": "true", "page_range": ""})
            self.assertIn("Hallo Welt", pdf_text(exported))
        finally:
            with main.JOBS_LOCK:
                main.JOBS.clear()
                main.JOB_RUNNERS.clear()
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_run_pdf_layout_translate_job_falls_back_to_plain_pipeline_for_scanned_pdfs(self):
        # extract_pdf_layout raises 422 when a PDF has no positioned text at all (scanned/
        # image-only). The layout job used to just fail there; the plain pipeline has an OCR
        # fallback, so it now takes over transparently instead of the user needing a "Plaintext"
        # checkbox to pick it themselves.
        temp_dir = test_temp_dir()
        try:
            with main.JOBS_LOCK:
                main.JOBS.clear()
                main.JOB_RUNNERS.clear()
            job_id = main.create_job("translate-pdf-layout", "eng_Latn", "deu_Latn", "scan.pdf")
            no_positioned_text = HTTPException(status_code=422, detail="No positioned text found.")
            with patch.object(main, "extract_pdf_layout", side_effect=no_positioned_text):
                with patch.object(main, "run_pdf_translate_job") as fallback_mock:
                    main.run_pdf_layout_translate_job(job_id, b"content", "eng_Latn", "deu_Latn", "scan.pdf", "1-2")

            fallback_mock.assert_called_once_with(
                job_id, b"content", "application/pdf", "eng_Latn", "deu_Latn", "scan.pdf", "1-2", layout_fallback=True
            )
        finally:
            with main.JOBS_LOCK:
                main.JOBS.clear()
                main.JOB_RUNNERS.clear()
            shutil.rmtree(temp_dir, ignore_errors=True)

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

    def test_history_download_name_carries_document_date_and_language(self):
        item = {
            "original_name": "Geschäftsbedingungen.pdf",
            "created_at": "2026-08-10T09:54:00+00:00",
            "target": "eng_Latn",
        }

        with patch.object(main, "HISTORY_TIMEZONE", "UTC"):
            name = main.history_download_name(item, "pdf")

        # Sanitised, but every part a reader needs to tell two downloads apart is in there, and
        # the umlaut is transliterated rather than replaced with a dash.
        self.assertEqual(name, "Geschaftsbedingungen_2026-08-10_0954_eng.pdf")
        self.assertRegex(name, r"^[A-Za-z0-9_.-]+$")

    def test_history_download_name_uses_the_configured_timezone(self):
        item = {"original_name": "x.pdf", "created_at": "2026-08-10T09:54:00+00:00", "target": "eng_Latn"}

        with patch.object(main, "HISTORY_TIMEZONE", "Europe/Berlin"):
            self.assertIn("_1154_", main.history_download_name(item, "pdf"))
        with patch.object(main, "HISTORY_TIMEZONE", "Not/AZone"):
            # An unusable zone must not break the download, it just leaves the time as stored.
            self.assertIn("_0954_", main.history_download_name(item, "pdf"))

    def test_history_download_name_survives_missing_metadata(self):
        # cleanup_history deletes files one by one, so a result can outlive its metadata. The
        # download must still produce a name rather than failing.
        name = main.history_download_name({}, "md")

        self.assertEqual(name, "translation_undated_translated.md")

    def test_history_export_names_the_file_after_the_document(self):
        temp_dir = test_temp_dir()
        try:
            item_id = "2026-08-02-text-abc123"
            (temp_dir / f"{item_id}.md").write_text("Translated result", encoding="utf-8")
            (temp_dir / f"{item_id}.json").write_text(json.dumps({
                "id": item_id,
                "original_name": "Vertrag.pdf",
                "created_at": "2026-08-02T14:05:00+00:00",
                "target": "eng_Latn",
            }), encoding="utf-8")

            with patch.object(main, "HISTORY_DIR", temp_dir):
                with patch.object(main, "HISTORY_TIMEZONE", "UTC"):
                    response = TestClient(main.app).get(f"/history/{item_id}/export?format=txt")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Vertrag_2026-08-02_1405_eng.txt", response.headers["content-disposition"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

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
            self.assertTrue(pdf_response.content.startswith(b"%PDF"))
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
            main.configure_torch_threads(torch_module)

        self.assertEqual(torch_module.threads, 3)

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
