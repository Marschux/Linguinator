import base64
import json
import os
import shutil
import sys
import tempfile
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
    characters = [{"c": char, "origin": (x + 0.5 * size * index, y),
                   "bbox": (x + 0.5 * size * index, y - size,
                            x + 0.5 * size * (index + 1), y + 0.2 * size)}
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
        self.rect = pymupdf.Rect(0, 0, 600.0, height)
        self.mediabox = self.rect

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
        self.assertIn('value="de">', template)
        self.assertIn("Deutsch", template)
        self.assertNotIn("previewToggle", template)
        self.assertNotIn("historyToggle", template)
        # Queue and history are one panel, and the progress bar above the buttons is gone with
        # it: a running job is shown by its own row's ring, nowhere else.
        self.assertIn('id="jobsPanel"', template)
        # Both were once deleted along with a neighbouring block, and the Translate button went
        # dead with them: every click ended in a ReferenceError before the upload.
        self.assertIn("function queueRing(", script)
        self.assertIn("function statusRowElement(", script)
        self.assertNotIn('id="progress"', template)
        self.assertNotIn("ownJobBanner", template)
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

    def test_one_invented_sentence_does_not_take_the_paragraph_with_it(self):
        # The model invents per model call, and a call is one sentence. Guarding only the joined
        # paragraph threw away a good translation because one sentence in it had run off.
        answers = iter(["Der erste Satz.",
                        "Der Präsident. — Das Wort hat die Fraktion der Europäischen Volkspartei."])
        tokenizer = FakeTokenizer()
        tokenizer.batch_decode = lambda generated, skip_special_tokens: [next(answers)]

        with patch.object(main, "load_model", return_value=(tokenizer, FakeModel(), "cpu", FakeTorch())):
            translated = main.translate_one("The first sentence. Watch TV.", "eng_Latn", "deu_Latn")

        self.assertEqual(translated, "Der erste Satz. Watch TV.")

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

    def test_ensure_known_language_rejects_anything_not_a_core_language(self):
        main.ensure_known_language("deu_Latn")
        main.ensure_known_language(main.AUTO_SOURCE, allow_auto=True)
        with self.assertRaises(HTTPException):
            main.ensure_known_language(main.AUTO_SOURCE)
        with self.assertRaises(HTTPException):
            # The short ISO code, not the internal deu_Latn-style one - what a direct API call
            # (bypassing the UI's own dropdown, which always sends the right one) could send.
            main.ensure_known_language("de")

    def test_translate_job_route_rejects_an_unknown_target(self):
        response = TestClient(main.app).post("/jobs/translate", json={"q": "Hallo", "target": "de"})
        self.assertEqual(response.status_code, 400)

    def test_ocr_language_code_maps_source_language_to_tesseract(self):
        with patch.object(main, "installed_ocr_languages", return_value=("deu", "eng", "fra", "chi_sim")):
            self.assertEqual(main.ocr_language_code("deu_Latn"), "deu")
            self.assertEqual(main.ocr_language_code("fra_Latn"), "fra")
            # Tesseract names this differently than we do.
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
            # Hebrew has no CORE_LANGUAGES entry (removed 14.08.2026), so nothing in that script
            # can be named - the empty result falls back to English same as an unknown script.
            self.assertEqual(main.ocr_probe_languages("Hebrew"), "eng")
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

    def test_ocr_page_osd_reads_the_confidence_too(self):
        report = "Page number: 0\nOrientation in degrees: 0\nScript: Cyrillic\nScript confidence: 3.4\n"
        with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout=report)):
            self.assertEqual(main.ocr_page_osd(Path("page.png")), ("Cyrillic", 3.4))

    def test_ocr_page_osd_stays_quiet_when_osd_fails(self):
        with patch.object(main.subprocess, "run", side_effect=OSError("no osd")):
            self.assertEqual(main.ocr_page_osd(Path("page.png")), ("", 0.0))

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
                with patch.object(main, "ocr_page_osd", return_value=("Hebrew", 12.22)):
                    self.assertFalse(main.text_layer_is_trustworthy(b"%PDF", 1, "hinmrg tilrdph"))
                with patch.object(main, "ocr_page_osd", return_value=("Latin", 4.17)):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "hinmrg tilrdph"))
                # Japanese is written with Han characters, so those two never contradict.
                with patch.object(main, "ocr_page_osd", return_value=("Japanese", 10.0)):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "作成日 東京都"))
                # OSD silent, or a page without letters: nothing to contradict.
                with patch.object(main, "ocr_page_osd", return_value=("", 0.0)):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "hinmrg tilrdph"))
                with patch.object(main, "ocr_page_osd", return_value=("Hebrew", 12.22)):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "12345"))

    def test_text_layer_is_trusted_when_osd_has_too_little_signal(self):
        # A numbers-heavy German table page: OSD misreads it as Cyrillic, but at confidence
        # 0.42 and 1.67 - measured on the real fixture, too little real-letter signal to mean
        # anything, unlike a genuinely broken page (Hebrew at 2.50-12.22).
        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "render_pdf_page", return_value=Path("page.png")):
                with patch.object(main, "ocr_page_osd", return_value=("Cyrillic", 0.42)):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "Tisch 12,50 EUR"))
                with patch.object(main, "ocr_page_osd", return_value=("Cyrillic", 1.67)):
                    self.assertTrue(main.text_layer_is_trustworthy(b"%PDF", 1, "Tisch 12,50 EUR"))
                # Right at and above the threshold, a genuine mismatch is still caught.
                with patch.object(main, "ocr_page_osd", return_value=("Hebrew", 2.50)):
                    self.assertFalse(main.text_layer_is_trustworthy(b"%PDF", 1, "hinmrg tilrdph"))

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
        detections = iter(["dan_Latn", "swe_Latn", "swe_Latn"])

        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "installed_ocr_languages", return_value=("swe", "deu", "eng", "fra", "dan")):
                with patch.object(main, "ocr_page_script", return_value="Latin"):
                    with patch.object(main, "detect_source_language", side_effect=lambda _text: next(detections)):
                        with patch.object(main, "run_tesseract", side_effect=["probe", "danish read", "swedish read"]) as reads:
                            with patch.object(main.subprocess, "run", return_value=SimpleNamespace(stdout="")):
                                text = main.ocr_pdf_page(b"%PDF", 1)

        self.assertEqual(text, "swedish read")
        self.assertEqual([call.args[1] for call in reads.call_args_list], ["eng+deu+fra", "dan", "swe"])

    def test_auto_detect_skips_the_second_read_when_the_probe_was_one_language(self):
        with patch.object(main.shutil, "which", return_value="/usr/bin/tesseract"):
            with patch.object(main, "installed_ocr_languages", return_value=("eng", "jpn")):
                with patch.object(main, "ocr_page_script", return_value="Japanese"):
                    with patch.object(main, "run_tesseract", return_value="テキスト") as reads:
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

    def test_image_line_bands_wraps_around_a_partial_width_image(self):
        # Image on the left half of a 200pt-wide body; the free gap is on the right.
        image = {"x": 0.0, "right": 100.0, "top": 200.0, "bottom": 100.0}
        bands = main.image_line_bands([image], width=200.0, margin=0.0, top=200.0, line_height=10.0)

        self.assertEqual(bands[0], (100.0, 100.0))
        # Past the image's bottom, back to the full body width.
        self.assertEqual(bands[-1], (0.0, 200.0))

    def test_image_line_bands_takes_the_widest_gap_between_two_images(self):
        left = {"x": 0.0, "right": 60.0, "top": 200.0, "bottom": 150.0}
        right = {"x": 260.0, "right": 300.0, "top": 200.0, "bottom": 150.0}
        bands = main.image_line_bands([left, right], width=300.0, margin=0.0, top=200.0, line_height=10.0)

        self.assertEqual(bands[0], (60.0, 200.0))

    def test_pdf_image_blocks_width_is_true_for_a_full_page_image_only(self):
        full_page = {"x": 0.0, "right": 300.0, "top": 200.0, "bottom": 100.0}
        self.assertTrue(main.pdf_image_blocks_width(full_page, width=300.0, margin=0.0))

        left_half = {"x": 0.0, "right": 150.0, "top": 200.0, "bottom": 100.0}
        self.assertFalse(main.pdf_image_blocks_width(left_half, width=300.0, margin=0.0))

    def test_pdf_document_pages_drops_a_full_page_image_and_keeps_plain_text(self):
        # A source page whose only image blocks the whole width (a full-page scan) gets the
        # plain, image-less text export instead of every line being pushed onto a page of its
        # own below the picture - see pdf_image_blocks_width.
        width, height, margin = 300.0, 400.0, 20.0
        wide_image = {"bytes": b"fake", "x": margin, "right": width - margin,
                     "top": height - margin - 24, "bottom": height - margin - 24 - 50}
        text = "# Page 1\n\nShort translated body text."
        pages = main.pdf_document_pages(text, {"1": {"images": [wide_image], "width": width, "height": height}})

        self.assertEqual(len(pages), 1)
        self.assertNotIn("images", pages[0])
        self.assertNotIn("width", pages[0])

    def test_pdf_document_pages_keeps_a_partial_width_image_and_places_it_once(self):
        width, height, margin = 300.0, 400.0, 20.0
        narrow_image = {"bytes": b"fake", "x": margin, "right": margin + 100,
                        "top": height - margin - 24, "bottom": height - margin - 24 - 50}
        text = "# Page 1\n\n" + ("A long paragraph that overflows onto a second page. " * 40)
        pages = main.pdf_document_pages(text, {"1": {"images": [narrow_image], "width": width, "height": height}})

        self.assertEqual(pages[0]["images"], [narrow_image])
        self.assertTrue(pages[0]["continuation"] is False)
        self.assertTrue(len(pages) > 1)
        self.assertNotIn("images", pages[1])
        self.assertTrue(pages[1]["continuation"])

    def test_create_text_pdf_reinserts_the_source_pdfs_own_image(self):
        source = pymupdf.open()
        page = source.new_page(width=300, height=400)
        pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 40, 40), False)
        pix.set_rect(pix.irect, (200, 0, 0))
        page.insert_image(pymupdf.Rect(20, 20, 100, 100), pixmap=pix)
        source_content = source.tobytes()

        content = main.create_text_pdf("# Page 1\n\nTranslated body text.", source_content)

        self.assertEqual(len(pymupdf.open(stream=content, filetype="pdf")[0].get_images()), 1)
        self.assertIn("Translated body text.", pdf_text(content))

    def test_create_text_pdf_without_source_content_stays_image_free(self):
        content = main.create_text_pdf("# Page 1\n\nTranslated body text.")

        self.assertEqual(pymupdf.open(stream=content, filetype="pdf")[0].get_images(), [])

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

    def test_cleanup_history_takes_the_whole_entry_including_the_prepared_export(self):
        # The export is written when someone downloads it, long after the rest of the entry. Aging
        # each file on its own left the translated document behind once its entry was gone.
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory)
            base = "2026-01-01-doc.pdf-abcd1234"
            old = time.time() - 30 * 86400
            for suffix in (".json", ".md", ".source.pdf"):
                path = history / (base + suffix)
                path.write_text("x", encoding="utf-8")
                os.utime(path, (old, old))
            export = history / (base + ".export.pdf")
            export.write_text("freshly rendered", encoding="utf-8")
            kept = history / "2026-01-01-other.pdf-99999999.json"
            kept.write_text("{}", encoding="utf-8")

            with patch.object(main, "HISTORY_DIR", history), patch.object(main, "HISTORY_HOURS", 24):
                main.cleanup_history()

            self.assertFalse(export.exists())
            self.assertEqual([path.name for path in history.iterdir()], [kept.name])

    def test_cleanup_finished_jobs_keeps_the_ones_with_work_left(self):
        # Finished jobs were never dropped: 137 had collected on the test machine, kept in memory,
        # written out one file each and sent along with every poll.
        old = time.time() - 2 * main.FINISHED_JOB_RETENTION_SECONDS
        jobs = {
            "done": {"id": "done", "status": "complete", "finished_at": old},
            "fresh": {"id": "fresh", "status": "complete", "finished_at": time.time()},
            "waiting": {"id": "waiting", "status": "queued", "finished_at": None},
            "busy": {"id": "busy", "status": "running", "finished_at": None},
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(main.JOBS, jobs, clear=True), patch.object(main, "JOBS_DIR", Path(directory)):
                main.cleanup_finished_jobs()
                remaining = set(main.JOBS)

        self.assertEqual(remaining, {"fresh", "waiting", "busy"})

    def test_list_jobs_leaves_the_translated_text_out(self):
        # The UI polls /jobs every few seconds and shows none of the result; carrying it along
        # made the answer 136 KB on a machine with a few finished documents on it.
        job = {"id": "j1", "status": "complete", "queued_at": time.time(),
               "finished_at": time.time(), "result": "a whole translated document"}
        with patch.dict(main.JOBS, {"j1": job}, clear=True):
            listed = main.list_jobs()
            single = main.public_job(main.JOBS["j1"])

        self.assertNotIn("result", listed[0])
        # The single-job route still hands it back, which is where the UI reads it from.
        self.assertEqual(single["result"], "a whole translated document")

    def test_history_original_export_is_built_once_and_reused(self):
        # Rebuilding a layout PDF per download cost seconds before the button did anything
        # (measured 2.8 s for 12 pages). The second call must come off the disk.
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(main, "HISTORY_DIR", Path(directory)):
                with patch.object(main, "export_original_history_content",
                                  return_value=b"rendered pdf") as export_mock:
                    first = main.history_original_export("entry", "pdf", b"source", "text", {})
                    second = main.history_original_export("entry", "pdf", b"source", "text", {})

                export_mock.assert_called_once()
                self.assertEqual(first, b"rendered pdf")
                self.assertEqual(second, b"rendered pdf")
                self.assertTrue(main.history_export_path("entry", "pdf").exists())

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

    def test_split_run_at_rules_cuts_a_run_the_grid_runs_through(self):
        # MatterhornProtokoll paints two cells of a row in one text run, "Objekt   Mensch  -  ",
        # 0.59em apart - ordinary word spacing in justified text, so no measurement of the gap
        # can tell them apart. The rule drawn between them can.
        def characters(text, x, size=11.0):
            return [{"c": char, "bbox": (x + 6 * index, 0.0, x + 6 * (index + 1), size)}
                    for index, char in enumerate(text)]

        identity = pymupdf.Matrix(1, 0, 0, 1, 0, 0)
        # The grid line falls into the spaces between the two cells.
        grid = [{"x": 88.0, "bottom": 0.0, "top": 20.0}]
        rules = [{"x": 100.0, "bottom": 0.0, "top": 20.0}]

        parts = main.split_run_at_rules(characters("Objekt   Mensch", 40.0), 10.0, identity, grid)
        self.assertEqual([part["text"].strip() for part in parts], ["Objekt", "Mensch"])
        self.assertTrue(all(part["split"] for part in parts))

        # A cell whose text overhangs its own rule keeps together: cut there, the "Siehe" column
        # of that document came apart into "0" and "1-001".
        parts = main.split_run_at_rules(characters("01-001", 97.0), 10.0, identity, rules)
        self.assertEqual([part["text"] for part in parts], ["01-001"])

        # And a page without a grid is left exactly as it was.
        parts = main.split_run_at_rules(characters("Ein ganzer Satz", 40.0), 10.0, identity, [])
        self.assertEqual([part["text"] for part in parts], ["Ein ganzer Satz"])
        self.assertFalse(parts[0]["split"])

    def test_rule_between_measures_from_where_the_runs_begin(self):
        # A producer pads a cell with trailing spaces, so the first run reaches a point past its
        # own rule. Measured from the end of that run, the rule would fall outside the gap.
        rules = [{"x": 75.5, "bottom": 700.0, "top": 760.0}]
        self.assertTrue(main.rule_between(36.0, 78.4, 751.2, rules))
        self.assertFalse(main.rule_between(78.4, 120.0, 751.2, rules))
        self.assertFalse(main.rule_between(36.0, 78.4, 690.0, rules))

    def test_group_pdf_lines_splits_cells_that_sit_close_together(self):
        # Powerupall page 72: a two-column table with no rule between the columns, whose cells
        # stand 7.5pt apart at 11pt - well inside PDF_CELL_GAP. Every row was merged into one
        # line and reflowed across the table. The right column starting at the same x on row
        # after row is what tells the cells apart, see PDF_COLUMN_MIN_RUNS.
        runs = []
        for index, baseline in enumerate((700.0, 685.0, 670.0)):
            runs.append({"text": f"Frage {index}", "x": 95.4, "y": baseline,
                         "size": 11.0, "width": 208.6})
            runs.append({"text": f"Antwort {index}", "x": 311.5, "y": baseline,
                         "size": 11.0, "width": 200.0})

        lines = main.group_pdf_lines(runs)

        self.assertEqual([line["x"] for line in lines],
                         [95.4, 311.5, 95.4, 311.5, 95.4, 311.5])

    def test_group_pdf_lines_keeps_a_list_marker_with_its_text(self):
        # The mirror case: a marker hangs to the left of its own text, and its indent repeats
        # down the list just as a column does. Splitting there would leave every marker as a
        # paragraph of its own.
        runs = []
        for index, baseline in enumerate((700.0, 685.0, 670.0)):
            runs.append({"text": f"{index}.", "x": 84.8, "y": baseline,
                         "size": 11.0, "width": 12.0})
            runs.append({"text": "Punkt", "x": 100.8, "y": baseline,
                         "size": 11.0, "width": 30.0})

        lines = main.group_pdf_lines(runs)

        self.assertEqual([line["text"] for line in lines],
                         ["0. Punkt", "1. Punkt", "2. Punkt"])

    def test_paragraph_line_limits_stop_at_the_next_column_of_the_table(self):
        # MatterhornProtokoll page 4: a tall cell whose neighbours are empty on all but its first
        # baseline. With nothing beside those lines to measure against they were handed the
        # document's right margin, and a 300pt cell was reflowed to 550pt across two columns.
        cell = {"lines": [{"x": 82.0, "y": 579.0 - 12.6 * step, "right": 330.0, "size": 11.0}
                          for step in range(4)]}
        neighbour = {"lines": [{"x": 343.7, "y": 579.0, "right": 399.3, "size": 11.0}]}
        columns = main.page_column_walls(cell["lines"] + neighbour["lines"] + [
            {"x": 343.7, "y": 560.0, "right": 399.3, "size": 11.0},
            {"x": 343.7, "y": 540.0, "right": 399.3, "size": 11.0},
        ])

        limits = main.paragraph_line_limits(cell, [cell, neighbour], 553.3, [], columns)

        self.assertEqual([round(limit, 1) for limit in limits], [341.7] * 4)

    def test_page_column_walls_ignore_a_first_line_indent(self):
        # The mirror case: body text starts at its indent often enough to look like a column,
        # but its own lines run straight across that indent, which a table's grid never is.
        lines = [{"x": 100.8, "y": 700.0, "right": 550.0, "size": 11.0}]
        lines += [{"x": 64.8, "y": 700.0 - 15.0 * step, "right": 550.0, "size": 11.0}
                  for step in range(1, 6)]
        lines += [{"x": 100.8, "y": 610.0 - 15.0 * step, "right": 550.0, "size": 11.0}
                  for step in range(2)]

        self.assertEqual([round(x, 1) for x, _, _ in main.page_column_walls(lines)], [64.8])

    def test_paragraph_line_limits_ignore_the_inset_of_a_box_far_to_the_left(self):
        # Systemrequirements: the "32 GB unified memory" cell is enclosed both by its own cell
        # and by the background of the whole table row, which starts 300pt further left. Mirroring
        # that distance as an inset (capped at PDF_LAYOUT_MAX_INDENT) took 40pt off the cell's
        # right edge, wrapped a one-line cell into three and pushed them out under the row.
        paragraph = {"lines": [{"x": 369.0, "y": 492.1, "right": 452.6, "size": 8.5}]}
        row = {"x": 61.5, "right": 458.4, "top": 532.5, "bottom": 490.0}
        cell = {"x": 365.2, "right": 458.4, "top": 532.5, "bottom": 490.0}

        limits = main.paragraph_line_limits(paragraph, [paragraph], 452.5, [row, cell])

        # The cell's own inset (3.8pt) still applies, the row's 307pt does not.
        self.assertAlmostEqual(limits[0], 452.6, places=1)

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

    def test_group_pdf_paragraphs_reads_a_double_spaced_page(self):
        # The dedication page of the test document runs at 2.35 times its 11pt and opens with an
        # indented line: every line of it came out as a paragraph of its own, so the model was
        # handed each of them without the rest of its sentence.
        def line(text, x, y):
            return {"text": text, "x": x, "y": y, "right": 550.0, "size": 11.0}

        paragraphs = main.group_pdf_paragraphs([
            line("This book is dedicated to those", 100.8, 642.1),
            line("burdened with the incalculable", 64.8, 616.2),
            line("weight of ME/CFS. It takes", 64.8, 590.4),
            line("great courage to face each day.", 64.8, 564.6),
            # Two blank lines further down: a new paragraph even at this leading.
            line("Thanks to my wife.", 64.8, 486.0),
        ])

        self.assertEqual([len(paragraph["lines"]) for paragraph in paragraphs], [4, 1])

    def test_paragraph_is_justified_only_where_the_original_was(self):
        def paragraph(*edges):
            return {"lines": [{"text": "x", "x": 60.0, "y": 700.0 - 14 * i, "right": edge, "size": 11.0}
                              for i, edge in enumerate(edges)]}

        # Flush to the eye: the last glyph of a line carries its own side bearing.
        self.assertTrue(main.paragraph_is_justified(paragraph(551.1, 547.4, 551.1, 120.0)))
        # Two lines that happen to end together say nothing.
        self.assertFalse(main.paragraph_is_justified(paragraph(551.1, 120.0)))
        # Ragged right.
        self.assertFalse(main.paragraph_is_justified(paragraph(551.1, 498.2, 530.0, 120.0)))
        # The last line is short by nature and is not counted.
        self.assertTrue(main.paragraph_is_justified(paragraph(551.1, 551.1, 200.0)))

    def test_reflow_paragraph_marks_a_justified_paragraph_for_the_writer(self):
        def paragraph(*edges):
            return {"lines": [{"text": "Some text of the original", "x": 60.0, "y": 700.0 - 14 * i,
                               "right": edge, "size": 11.0} for i, edge in enumerate(edges)]}

        text = "Eine Uebersetzung, die ueber mehrere Zeilen laeuft und dabei genug Woerter "
        placed = main.reflow_paragraph(paragraph(400.0, 400.0, 400.0, 200.0), text * 2)

        self.assertTrue(all(line["justify_to"] == 400.0 for line in placed[:-1]))
        # The closing line of a paragraph is set ragged, as it is in any book.
        self.assertNotIn("justify_to", placed[-1])
        # A ragged original stays ragged.
        ragged = main.reflow_paragraph(paragraph(400.0, 330.0, 380.0, 200.0), text * 2)
        self.assertTrue(all("justify_to" not in line for line in ragged))

    def test_a_justified_line_reaches_the_edge_and_stays_readable(self):
        page = {
            "width": 400, "height": 200, "margin": 20,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [{"text": "Diese Zeile wird bis an die rechte Kante gestreckt und bleibt",
                       "font": "F1", "size": 11,
                       "line_height": 14, "x": 40, "y": 150, "justify_to": 362.8}],
        }

        pdf = main.create_pdf_from_pages([page])

        spans = pdf_spans(pdf)
        self.assertAlmostEqual(max(span["bbox"][2] for span in spans), 362.8, delta=1.5)
        # Set word by word, so the text layer has to be checked, not assumed: a gap wide enough
        # makes MuPDF return every word on a line of its own.
        self.assertIn("Diese Zeile wird bis an die rechte Kante gestreckt und bleibt", pdf_text(pdf))

    def test_a_line_needing_too_much_stretch_stays_ragged(self):
        # Wider than PDF_JUSTIFY_MAX_SPACE would tear holes into the setting and, past about four
        # times the normal gap, take the text layer apart with it.
        page = {
            "width": 400, "height": 200, "margin": 20,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [{"text": "Drei kurze Woerter", "font": "F1", "size": 11,
                       "line_height": 14, "x": 40, "y": 150, "justify_to": 360.0}],
        }

        spans = pdf_spans(main.create_pdf_from_pages([page]))

        self.assertLess(max(span["bbox"][2] for span in spans), 300.0)

    def test_reflow_paragraph_keeps_the_first_line_indent(self):
        # Pages like this set their paragraphs without a blank line between them, so the indent
        # is the only thing showing where one ends.
        paragraph = {"lines": [
            {"text": "An indented opening line of", "x": 100.0, "y": 700.0, "right": 300.0, "size": 11.0},
            {"text": "a paragraph, then the body.", "x": 64.0, "y": 686.0, "right": 300.0, "size": 11.0},
        ]}

        placed = main.reflow_paragraph(paragraph, "Eine eingerueckte erste Zeile, dann der Rumpf.")

        self.assertEqual(placed[0]["x"], 100.0)
        self.assertTrue(all(line["x"] == 64.0 for line in placed[1:]))

    def test_group_pdf_paragraphs_reads_a_numbered_list(self):
        # A numbered list is set with the marker hanging out to the left of its own text, and
        # consecutive one-line items look exactly like a wrapped paragraph by geometry alone:
        # the whole list came out as one block of prose.
        def line(text, x, y):
            return {"text": text, "x": x, "y": y, "right": 500.0, "size": 11.0}

        paragraphs = main.group_pdf_paragraphs([
            line("1. Keep a regular sleep schedule. Go to sleep", 82.8, 212.1),
            line("at the same time each morning.", 100.8, 199.2),
            line("2. Avoid napping after 3 p.m.", 82.8, 186.3),
            line("3. Avoid caffeine late in the day.", 82.8, 173.4),
        ])

        self.assertEqual([paragraph["text"] for paragraph in paragraphs], [
            "1. Keep a regular sleep schedule. Go to sleep at the same time each morning.",
            "2. Avoid napping after 3 p.m.",
            "3. Avoid caffeine late in the day.",
        ])

    def test_typical_line_spacing_takes_the_smallest_repeated_gap(self):
        # Not the average or the most common one: a page of short paragraphs has more gaps
        # between paragraphs than inside them, and those would then count as the leading.
        def line(y):
            return {"text": "x", "x": 50.0, "y": y, "right": 100.0, "size": 10.0}

        self.assertEqual(main.typical_line_spacing(
            [line(700), line(685), line(658), line(643), line(616), line(589)]), 15.0)
        # Nothing repeats, so no page leading can be read off it.
        self.assertEqual(main.typical_line_spacing([line(700), line(680), line(650)]), 0.0)

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

    def test_reflow_paragraph_keeps_its_leading_on_a_shrunken_overflow_line(self):
        # A shrunken paragraph still sets its first lines on the original's own baselines, so
        # spacing the overflow lines in proportion to the shrinking left the last line at a
        # different distance than every line above it: measured on Powerupall p. 8, a paragraph
        # set at 0.77 ran at 15.0pt throughout and then closed at 11.6.
        paragraph = {"lines": [
            {"text": "One", "x": 50.0, "y": 700.0, "right": 200.0, "size": 22.0},
            {"text": "two", "x": 50.0, "y": 660.0, "right": 200.0, "size": 22.0},
        ]}

        placed = main.reflow_paragraph(paragraph, "Eine deutlich laengere Uebersetzung " * 4)

        self.assertGreater(len(placed), 2)
        self.assertLess(placed[0]["size"], 22.0)  # it did have to shrink
        overflow_steps = [a["y"] - b["y"] for a, b in zip(placed[1:], placed[2:])]
        for step in overflow_steps:
            self.assertAlmostEqual(step, 40.0, places=2)

    def test_reflow_paragraph_keeps_its_own_leading_on_an_unshrunk_overflow_line(self):
        # A paragraph that did not have to shrink kept its leading everywhere except on the
        # overflow line, which sat visibly tighter than the rest (Powerupall p. 6 "Hoffnung").
        paragraph = {"lines": [
            {"text": "One", "x": 50.0, "y": 700.0, "right": 400.0, "size": 11.0},
            {"text": "two", "x": 50.0, "y": 674.0, "right": 400.0, "size": 11.0},
        ]}

        # scale=1.0 is the page-wide size render_pdf_layout_overlay settles on: full size, and
        # the overflow line still has to keep the paragraph's own 26pt leading.
        placed = main.reflow_paragraph(paragraph, "Wort " * 60, floor=100.0, scale=1.0)

        self.assertGreater(len(placed), 2)
        self.assertEqual(placed[0]["size"], 11.0)
        self.assertAlmostEqual(placed[1]["y"] - placed[2]["y"], 26.0, places=2)

    def test_reflow_paragraph_leaves_a_real_gap_before_the_next_paragraph(self):
        # 1.15em of clearance only kept ascenders/descenders from touching the next paragraph's
        # own text - not enough to still read as a paragraph break. Landscape_Mixed_Pages page 3:
        # "...Betriebsdauer in aggressiven Meeresumgebungen bieten." ran straight into "Die
        # Umwandlung..." with no visible gap, both at normal reading size. Measured against the
        # original: paragraphs there sit 2.15x the font size apart, 1.35x for an ordinary line.
        paragraph = {"lines": [
            {"text": "One", "x": 50.0, "y": 700.0, "right": 400.0, "size": 10.0},
            {"text": "two", "x": 50.0, "y": 686.5, "right": 400.0, "size": 10.0},
        ]}
        floor = 640.0  # the next paragraph's own first-line baseline

        placed = main.reflow_paragraph(paragraph, "Eine deutlich laengere Uebersetzung " * 5, floor=floor)

        self.assertGreater(len(placed), 2)
        gap = placed[-1]["y"] - floor
        self.assertGreaterEqual(gap, 1.8 * placed[-1]["size"])

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
        # A lone character of a script that writes without spaces gives the model as little as a
        # lone letter does: 完 ("The End", the closing line of Hoshi no Kagi) came back as "wieso
        # ist das alles?". Two of them are a sentence and are translated.
        for fragment in ("完", "— 完 —", "あ"):
            with self.subTest(fragment=fragment):
                self.assertFalse(main.has_translatable_text(fragment))
        for real in ("Ja", "Hello world", "2. Scope", "16 GB", "日本語", "完了", "終わり",
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
        # Measured pairs from the test document: a chapter heading that came back as Europarl
        # boilerplate goes, the longest genuine growth on those pages stays.
        self.assertEqual(
            main.guard_hallucination("Chapter 1", "Kapitel 1 — ENTWICKLUNG UND ENTWICKLUNGEN"),
            "Chapter 1",
        )
        self.assertEqual(main.guard_hallucination("My Story", "Meine Geschichte"), "Meine Geschichte")
        self.assertEqual(
            main.guard_hallucination(
                "3. Watching television for 30 minutes",
                "3. Der Präsident. — Das Wort hat die Fraktion der Europäischen Demokraten. "
                "Fernsehen für 30 Minuten",
            ),
            "3. Watching television for 30 minutes",
        )

    def test_guard_hallucination_rejects_wiki_markup_the_source_never_had(self):
        # The author's name on the title page came back as the scaffolding of a Wikipedia article,
        # and at the same length as its source the length check cannot see it.
        self.assertEqual(
            main.guard_hallucination("Russ Seigenberg, Ph.D.", "== Weblinks ==== Einzelnachweise =="),
            "Russ Seigenberg, Ph.D.",
        )
        self.assertEqual(main.guard_hallucination("See [[Anchor]]", "Siehe [[Anker]]"), "Siehe [[Anker]]")
        self.assertEqual(main.guard_hallucination("a == b", "a == b"), "a == b")

    def test_guard_hallucination_rejects_a_stray_cjk_character(self):
        # MatterhornProtokoll's footer, a short line repeated on every page, came back "Competence
        # Center 的 PDF/UA-1" - same single invented character in the same spot on three pages.
        # Under the length guard and free of HALLUCINATION_MARKUP, nothing else catches it.
        self.assertEqual(
            main.guard_hallucination(
                "PDF Association PDF/UA-Kompetenzzentrum",
                "The PDF Association PDF/UA Competence Center 的 PDF/UA-1", "eng_Latn"),
            "PDF Association PDF/UA-Kompetenzzentrum",
        )
        # A source that already carries the character is left alone - it belongs to the document.
        self.assertEqual(main.guard_hallucination("見出し 的", "Heading 的", "eng_Latn"), "Heading 的")
        # And translating INTO Chinese or Japanese is not a hallucination just because the source
        # had none - that is the whole point of the translation.
        self.assertEqual(
            main.guard_hallucination("Chapter", "第一章", "zho_Hans"), "第一章")
        self.assertEqual(
            main.guard_hallucination("Chapter", "第一章", "jpn_Jpan"), "第一章")

    def test_guard_hallucination_allows_cjk_to_expand(self):
        # Han and kana carry a word in one or two characters, so a correct German translation is
        # several times its source in length. Measured pairs (fallback model, guard off, ja/zh -> de):
        # the guard used to discard all of these, which is why Hoshi_no_Kagi came back with its
        # headings and quote still in Japanese.
        for source, translated in (
            ("星の鍵と幻影の森", "Der Schlüssel zu den Sternen und der Schattenwald."),
            ("第六章：星の鍵の覚醒", "Kapitel 6: Das Auftauchen des Schlüssels der Sterne"),
            ("「千年に一度、星々が地に墜ちる夜、運命の鍵が覚醒する。」",
             '"Die Nacht, in der die Sterne einmal im Jahr auf die Erde fallen, '
             'die Nacht, in der der Schlüssel des Schicksals erwacht."'),
            ("系统要求", "Das System verlangt es."),
            ("星之钥与幻影之森", "Der Sternschlüssel und der Schatten."),
        ):
            self.assertEqual(main.guard_hallucination(source, translated), translated)

        # The extra room is not a free pass: a training-data dump off a CJK heading is still caught.
        heading = "第六章：星の鍵の覚醒"
        self.assertEqual(
            main.guard_hallucination(
                heading,
                "3. Der Präsident. — Das Wort hat die Fraktion der Europäischen Demokraten zu "
                "einer Frage der Geschäftsordnung. Ich erteile ihm das Wort.",
            ),
            heading,
        )

        # Hangul is deliberately not weighted: the fallback model answers Korean with Bible
        # boilerplate, and the unweighted budget is what keeps it off the page.
        korean = "세계가 아직 젊고 대기에 마법 입자가 가득했던 시대."
        self.assertEqual(
            main.guard_hallucination(
                korean,
                "Und es geschah, als der dritte Knabe diente, daß er auf dem Felde wohnte, "
                "und er sprach zu ihm: Siehe, ich bin bei dir.",
            ),
            korean,
        )

    def test_layout_overlay_leaves_wordless_fragments_untouched(self):
        # A fragment keeps its own original text rather than whatever came back in its
        # translation slot - guard_hallucination's length check cannot catch a short, plausible
        # invention for a fragment with nothing to translate. It is still redacted and redrawn
        # like any other paragraph, though (see test_layout_overlay_resizes_wordless_fragments_
        # to_match_their_table), so its own text has to survive that round trip unchanged.
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

    def test_layout_overlay_sets_one_size_across_the_whole_document(self):
        # A page whose translation fits at full size, followed by one that has to shrink. Settled
        # per page, the two came back in different sizes, which shows as body text changing size
        # from one page to the next (measured on Powerupall: 7.7 to 11.0 across its 92 text pages).
        def page(number, text):
            return {
                "number": number, "width": 400.0, "height": 300.0,
                "paragraphs": [{"text": text, "lines": [
                    {"text": text, "x": 50.0, "y": 200.0, "right": 350.0, "size": 11.0},
                ]}],
            }

        def source_page(text):
            return {
                "width": 400, "height": 300, "margin": 40,
                "source_page": "", "continuation": False, "footer": False,
                "lines": [{"text": text, "font": "F1", "size": 11, "line_height": 14,
                           "x": 50, "y": 200}],
            }

        pages = [page(1, "Short one"), page(2, "The second line here")]
        source = main.create_pdf_from_pages([source_page("Short one"),
                                             source_page("The second line here")])

        # The second translation is far longer than its original and forces a smaller size.
        overlay = main.render_pdf_layout_overlay(
            source, pages, ["Kurz", "Die zweite Zeile steht hier und ist erheblich länger als ihr "
                                    "Original, sodass sie kleiner gesetzt werden muss"])

        sizes = {round(span["size"], 1) for number in (0, 1)
                 for span in pdf_spans(overlay, number) if span["text"].strip()}

        self.assertEqual(len(sizes), 1, f"body text set in several sizes: {sorted(sizes)}")

    def test_layout_overlay_sets_the_cells_of_a_row_in_one_size(self):
        # Powerupall page 50: two cells of one row set 11.0 against 12.0, which at the scale the
        # book ends up in reads as two sizes in one row rather than as a size difference. The
        # heading above keeps its own size, being far enough off to be meant.
        pages = [{
            "number": 1, "width": 400.0, "height": 300.0,
            "paragraphs": [
                {"text": "Heading", "lines": [
                    {"text": "Heading", "x": 50.0, "y": 260.0, "right": 150.0, "size": 16.0},
                ]},
                {"text": "Left cell", "lines": [
                    {"text": "Left cell", "x": 50.0, "y": 200.0, "right": 150.0, "size": 12.0},
                ]},
                {"text": "Right cell", "lines": [
                    {"text": "Right cell", "x": 200.0, "y": 194.0, "right": 300.0, "size": 11.0},
                ]},
            ],
        }]
        source = main.create_pdf_from_pages([{
            "width": 400, "height": 300, "margin": 40,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [{"text": text, "font": "F1", "size": size, "line_height": 14,
                       "x": x, "y": y}
                      for text, x, y, size in (("Heading", 50, 260, 16),
                                               ("Left cell", 50, 200, 12),
                                               ("Right cell", 200, 194, 11))],
        }])

        overlay = main.render_pdf_layout_overlay(
            source, pages, ["Überschrift", "Linke Zelle", "Rechte Zelle"])

        sizes = {span["text"].split()[0]: round(span["size"], 1)
                 for span in pdf_spans(overlay, 0) if span["text"].strip()}

        self.assertEqual(sizes["Linke"], sizes["Rechte"])
        self.assertNotEqual(sizes["Überschrift"], sizes["Linke"])

    def test_layout_overlay_resizes_wordless_fragments_to_match_their_table(self):
        # Landscape_Mixed_Pages: a table's numeric column ("4.8", "38", ...) has nothing to
        # translate, so it used to be skipped entirely by render_pdf_layout_overlay and
        # redact_translated_text alike - left at its own original size while the label cell
        # beside it shrank to fit a much longer translation, which read as the same "two sizes in
        # one row" fault test_layout_overlay_sets_the_cells_of_a_row_in_one_size guards against.
        # The fragment's own original text has to survive unchanged, though - see
        # test_layout_overlay_leaves_wordless_fragments_untouched - only its size may move.
        pages = [{
            "number": 1, "width": 400.0, "height": 300.0,
            "paragraphs": [
                {"text": "Label", "lines": [
                    {"text": "Label", "x": 50.0, "y": 200.0, "right": 150.0, "size": 11.0},
                ]},
                {"text": "42", "lines": [
                    {"text": "42", "x": 200.0, "y": 200.0, "right": 220.0, "size": 11.0},
                ]},
            ],
        }]
        source = main.create_pdf_from_pages([{
            "width": 400, "height": 300, "margin": 40,
            "source_page": "", "continuation": False, "footer": False,
            "lines": [{"text": text, "font": "F1", "size": 11, "line_height": 14, "x": x, "y": 200}
                      for text, x in (("Label", 50), ("42", 200))],
        }])

        # "Nein" stands in for whatever a hallucinated translation of "42" might have come back
        # as - guard_hallucination's length check would not catch it, has_translatable_text does.
        overlay = main.render_pdf_layout_overlay(
            source, pages, ["Beschriftung deutlich laenger als Original", "Nein"])

        spans = [span for span in pdf_spans(overlay, 0) if span["text"].strip()]
        numeral = next(span for span in spans if span["text"].strip() == "42")
        label_size = next(span["size"] for span in spans if span["text"].startswith("Beschriftung"))

        self.assertEqual(round(numeral["size"], 1), round(label_size, 1))
        self.assertNotIn("Nein", [span["text"] for span in spans])

    def test_level_table_sizes_pulls_a_whole_table_to_one_size(self):
        # Powerupall page 72: cells beside each other were levelled, rows above each other were
        # not, so the table alternated between two sizes row by row.
        # Widths vary between calls (ragged), not fixed to one column width - a fixed width would
        # itself look like a justified body-text column to pdf_layout_justified_columns and be
        # wrongly excluded, see test_level_table_sizes_ignores_a_justified_body_text_column.
        widths = iter((80, 95, 70, 88, 60, 92))

        def cell(left, baseline, size=11.0):
            return {"lines": [{"x": left, "y": baseline, "right": left + next(widths), "size": size}]}

        paragraphs = [cell(50, 740, 16.0),                      # heading above the table
                      cell(50, 700), cell(200, 700),            # first row
                      cell(50, 680), cell(200, 680),            # second row
                      cell(50, 400)]                            # body text further down
        bases = [16.0, 11.0, 11.0, 11.0, 11.0, 11.0]
        targets = [16.0, 8.0, 8.0, 6.0, 8.0, 9.0]

        main.level_table_sizes(paragraphs, bases, targets, [])

        self.assertEqual(targets[1:5], [6.0, 6.0, 6.0, 6.0])
        self.assertEqual(targets[0], 16.0)   # a heading is a size of its own
        self.assertEqual(targets[5], 9.0)    # body text is no part of the table

    def test_level_table_sizes_ignores_a_justified_body_text_column(self):
        # Two_Column_Paper page 6: a data table sits above two columns of running text. Both
        # columns' paragraphs shared close to the same vertical range - ordinary for two columns
        # of similar length - and satisfied the same "beside"/"same left edge, small gap" geometry
        # the table's own cells did, pulling the whole page down to the table's 3.8pt minimum. A
        # justified column (consistent right edge, unlike a table's ragged cells) is now excluded
        # from both relations that would otherwise link it in.
        def line(left, baseline, right, size=9.4):
            return {"x": left, "y": baseline, "right": right, "size": size}

        # Left column: several lines wide enough and consistent enough to read as justified body
        # text (>=100pt wide, most lines share the same right edge).
        left_lines = [line(52.0, y, 300.0) for y in (580.0, 566.0, 552.0, 538.0, 524.0)]
        # Right column, roughly the same vertical range as the left one - what used to trigger the
        # "beside" relation between the two columns.
        right_lines = [line(306.6, y, 555.0) for y in (581.4, 567.4, 553.4, 539.4, 525.4)]
        left_paragraph = {"lines": left_lines}
        right_paragraph = {"lines": right_lines}
        # A genuine table cell just below, ragged and narrow - what actually needed to shrink.
        cell_paragraph = {"lines": [line(55.0, 480.0, 116.0, size=7.8)]}

        paragraphs = [left_paragraph, right_paragraph, cell_paragraph]
        bases = [9.4, 9.4, 7.8]
        targets = [6.2, 6.6, 3.8]

        main.level_table_sizes(paragraphs, bases, targets, [])

        self.assertEqual(targets[0], 6.2)
        self.assertEqual(targets[1], 6.6)
        self.assertEqual(targets[2], 3.8)

    def test_level_table_sizes_holds_one_cell_to_one_size(self):
        # A cell whose text falls into several paragraphs came back with the first one larger
        # than the rest (Powerupall page 50, "Kreative und energetische …").
        box = {"x": 40.0, "right": 180.0, "top": 720.0, "bottom": 660.0}
        paragraphs = [{"lines": [{"x": 50.0, "y": 700.0, "right": 150.0, "size": 11.0}]},
                      {"lines": [{"x": 50.0, "y": 685.0, "right": 150.0, "size": 11.0}]}]
        targets = [9.0, 7.0]

        main.level_table_sizes(paragraphs, [11.0, 11.0], targets, [box])

        self.assertEqual(targets, [7.0, 7.0])

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

        limit = main.paragraph_width_limit(heading, [heading, below], 575.0)

        self.assertEqual(limit, 575.0)

    def test_paragraph_width_limit_stops_at_the_next_column(self):
        left = {"lines": [{"text": "cell", "x": 50.0, "y": 700.0, "right": 150.0, "size": 10.0}]}
        right = {"lines": [{"text": "other", "x": 300.0, "y": 700.0, "right": 400.0, "size": 10.0}]}

        self.assertEqual(main.paragraph_width_limit(left, [left, right], 575.0), 298.0)

    def test_document_right_margin_stays_inside_the_documents_own_text_edge(self):
        # A page edge limit pushed every paragraph past the margin the original kept. The lone
        # full-width rule must not hand its own edge to the rest of the page.
        page = {"width": 595.0, "paragraphs": [
            {"lines": [{"right": 540.0}] * 20},
            {"lines": [{"right": 590.0}]},
        ]}

        self.assertEqual(main.document_right_margin([page]), 540.0)

    def test_paragraph_width_limit_never_reports_less_than_the_text_itself(self):
        # A paragraph overlapping something to its right must still get its own width, not a
        # negative one.
        wide = {"lines": [{"text": "wide", "x": 50.0, "y": 700.0, "right": 400.0, "size": 10.0}]}
        overlapping = {"lines": [{"text": "x", "x": 60.0, "y": 700.0, "right": 70.0, "size": 10.0}]}

        self.assertEqual(main.paragraph_width_limit(wide, [wide, overlapping], 575.0), 400.0)

    def test_paragraph_width_limit_stops_at_an_image(self):
        # Text ran straight across the photographs of Stall-Kamera-System: an image is not text,
        # so nothing in the reflow knew it was there.
        paragraph = {"lines": [{"text": "caption", "x": 50.0, "y": 700.0, "right": 150.0, "size": 10.0}]}
        image = {"x": 300.0, "right": 560.0, "top": 760.0, "bottom": 640.0}

        # Half an em short of the image, so the text keeps a visible gap to it.
        self.assertEqual(main.paragraph_width_limit(paragraph, [paragraph], 575.0, [image]), 295.0)

    def test_paragraph_width_limit_stays_inside_the_box_it_sits_in(self):
        # A table cell with an empty neighbour let the translation run to the page margin,
        # outside the box it belongs to (Systemanforderungen).
        paragraph = {"lines": [{"text": "cell", "x": 70.0, "y": 700.0, "right": 120.0, "size": 10.0}]}
        cell = {"x": 62.0, "right": 240.0, "top": 710.0, "bottom": 690.0}

        # The text is inset 8pt from the left of its box, so it stops 8pt short of the right of
        # it: filling to within 2pt read as text pressed against one side of a padded box.
        self.assertEqual(main.paragraph_width_limit(paragraph, [paragraph], 575.0, [cell]), 232.0)

    def test_paragraph_width_limit_stops_at_the_cell_the_original_overran(self):
        # The original's own text already runs past its cell into the next column
        # (Systemrequirements, "Graphics card"). Keeping that width handed the whole table to the
        # translation, which is longer still. The cell the line starts in wins over its own ink.
        paragraph = {"lines": [{"text": "runs on", "x": 143.0, "y": 512.0, "right": 430.0, "size": 8.5}]}
        cell = {"x": 140.0, "right": 362.0, "top": 525.0, "bottom": 490.0}

        # 3pt in from the left of the cell, so 3pt short of its right edge.
        self.assertEqual(main.paragraph_width_limit(paragraph, [paragraph], 542.0, [cell]), 359.0)

    def test_paragraph_width_limit_ignores_a_shape_beside_a_short_line(self):
        # A paragraph opening with a one-word line has all sorts of things to the right of that
        # line that say nothing about its width - taken as a wall, the whole paragraph was set
        # one word per line (Systemrequirements, "or / Apple M1 SoC GPU ...").
        paragraph = {"lines": [
            {"text": "or", "x": 143.0, "y": 502.0, "right": 151.0, "size": 8.5},
            {"text": "Apple M1 SoC GPU with 8 or more cores", "x": 143.0, "y": 492.0,
             "right": 339.0, "size": 8.5},
        ]}
        rule = {"x": 151.5, "right": 160.0, "top": 505.0, "bottom": 500.0}

        # 8.5pt of rule beside a 196pt paragraph narrows neither of its lines, so both keep the
        # page's own margin. Nothing here is a real edge - the cell that bounds this paragraph in
        # the document is a separate obstacle, see the test above.
        self.assertEqual(main.paragraph_line_limits(paragraph, [paragraph], 542.0, [rule]),
                         [542.0, 542.0])

    def test_reflow_paragraph_keeps_an_italic_paragraph_italic(self):
        # A pulled quote or a caption set wholly in italic came back upright: the reflow only ever
        # voted on weight and serif. 111 lines across the eight test documents are set this way.
        quote = {"lines": [
            {"text": "The gods plant reason in mankind,", "x": 150.0, "y": 700.0, "right": 400.0,
             "size": 11.0, "italic": True},
            {"text": "of all good gifts the highest.", "x": 150.0, "y": 686.0, "right": 380.0,
             "size": 11.0, "italic": True},
        ]}
        placed = main.reflow_paragraph(quote, "Die Goetter pflanzen Vernunft in die Menschheit.")
        self.assertEqual(placed[0]["font"], "F5")
        self.assertEqual(main.PDF_FONT_FACES["F5"], (False, False, True))

        # A word or two of italic inside a sentence does not carry over: which words of the
        # translation it would apply to is not knowable, so the line stays upright.
        mixed = {"lines": [
            {"text": "he wrote a book entitled Emotional Intelligence in 1995", "x": 65.0,
             "y": 700.0, "right": 400.0, "size": 11.0, "italic": False},
        ]}
        self.assertEqual(main.reflow_paragraph(mixed, "Er schrieb 1995 ein Buch")[0]["font"], "F1")

    def test_font_file_falls_back_to_upright_when_no_italic_is_installed(self):
        # The image ships fonts-dejavu-core, which has no italic cut. Falling through to the
        # base-14 stand-in there would cost a Cyrillic or Greek document its glyphs, so the
        # upright file of the same family is used instead and only the slant is lost.
        with patch.object(main, "PDF_FONT_ITALIC_CANDIDATES", ("/nonexistent/italic.ttf",)):
            main.font_file.cache_clear()
            self.assertEqual(main.font_file(False, "", False, True),
                             main.font_file(False, "", False, False))
        main.font_file.cache_clear()

    def test_paragraph_line_limits_set_a_paragraph_around_a_picture(self):
        # Stall-Kamera-System page 1: the image to the right reaches only the upper lines of the
        # paragraph. Those end at 215 and under in the original, the lines below the image run on
        # to 237, and one width for the block handed the widest of them to the lines beside the
        # picture, setting them into it.
        paragraph = {"lines": [
            {"text": "beside the picture", "x": 106.0, "y": 355.6, "right": 214.2, "size": 9.0},
            {"text": "still beside it", "x": 106.0, "y": 341.0, "right": 201.5, "size": 9.0},
            {"text": "below it now, running wider", "x": 106.0, "y": 283.1, "right": 237.1,
             "size": 9.0},
        ]}
        image = {"x": 227.3, "right": 397.0, "top": 428.6, "bottom": 301.9}

        limits = main.paragraph_line_limits(paragraph, [paragraph], 540.0, [image])

        self.assertEqual(limits[:2], [222.8, 222.8])  # stopped half an em short of the image
        self.assertEqual(limits[2], 540.0)  # clear of it, out to the margin

        # And the reflow wraps each line to its own edge rather than to the widest of them.
        placed = main.reflow_paragraph(paragraph, "Wort " * 40, width_limit=limits)
        for line in placed[:2]:
            self.assertLessEqual(
                line["x"] + main.pdf_measure_text(line["text"], line["size"], False, False), 222.8)

    def test_expand_lowercase_ligatures_repairs_the_broken_glyph(self):
        # Stall-Kamera-System draws "abrufbar" with an fb ligature its font maps to ĩ, which is
        # also an ordinary Vietnamese letter - LIGATURE_NATIVE_LANGUAGES exists for that carve-out
        # but is empty now that Vietnamese is no longer offered (see CORE_LANGUAGES), so every
        # source repairs it.
        self.assertEqual(main.expand_lowercase_ligatures("abruĩar", "deu_Latn"), "abrufbar")
        # Only inside a word: standing alone or at an edge it is a letter, not a ligature.
        self.assertEqual(main.expand_lowercase_ligatures("ĩ und Mĩ", "deu_Latn"), "ĩ und Mĩ")

    def test_expand_pdf_ligatures_puts_the_letters_back(self):
        # Presentation forms, which the model has never seen and which reach the finished
        # document unchanged.
        self.assertEqual(main.expand_pdf_ligatures("Anschaﬀung"), "Anschaffung")
        self.assertEqual(main.expand_pdf_ligatures("Traﬃc"), "Traffic")

        # The same ligatures from a producer whose ToUnicode points them at Latin Extended-B
        # (Stall-Kamera-System). "läuŌ" reached the translation and came back as "läuÅ".
        self.assertEqual(main.expand_pdf_ligatures("läuŌ"), "läuft")
        self.assertEqual(main.expand_pdf_ligatures("PosiƟon"), "Position")
        self.assertEqual(main.expand_pdf_ligatures("FestplaƩe"), "Festplatte")

        # Every one of those is a real capital elsewhere: Ō carries the macron of Latin and of
        # romanised Japanese, both of which this translates. Only a lowercase letter in front of
        # it makes it a ligature, so a word opening with one is left alone.
        self.assertEqual(main.expand_pdf_ligatures("Ōsaka"), "Ōsaka")
        self.assertEqual(main.expand_pdf_ligatures("ŌTIUM"), "ŌTIUM")
        # Lowercase mis-mappings are left alone: ĩ is an ordinary Vietnamese letter.
        self.assertEqual(main.expand_pdf_ligatures("nghĩ"), "nghĩ")

    def test_group_pdf_lines_keeps_a_caption_off_its_neighbours_line(self):
        # Powerupall page 86: a caption on its own tinted panel came within 21.4pt of the body
        # line beside it, just inside PDF_CELL_GAP, so the two merged into one line. That line
        # then reached across the panel and the translation was drawn over it.
        runs = [
            {"text": "often use labels such as", "x": 64.8, "y": 221.3, "width": 327.7,
             "size": 11.0},
            {"text": "to", "x": 400.0, "y": 221.3, "width": 13.7, "size": 11.0},
            {"text": "No one is stupid!", "x": 435.1, "y": 221.3, "width": 96.7, "size": 11.0},
        ]
        panel = [{"x": 419.6, "right": 547.2, "top": 244.1, "bottom": 103.9}]

        # 21.4pt of gap alone keeps them together, which is what went wrong.
        self.assertEqual(len(main.group_pdf_lines([dict(r) for r in runs])), 1)
        # The panel edge between them separates them regardless of the gap.
        lines = main.group_pdf_lines([dict(r) for r in runs], panel)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["text"], "No one is stupid!")

    def test_group_pdf_paragraphs_splits_on_a_change_of_weight(self):
        # Get_Started_With_Smallpdf sets its headings bold at the body size, so the size check
        # alone kept them in the paragraph below, where they lost their own weight and alignment.
        lines = [
            {"text": "Digital Documents-All In One Place", "x": 289.0, "y": 694.0,
             "right": 508.0, "size": 14.0, "bold": True},
            {"text": "With the new Smallpdf experience, you can", "x": 289.0, "y": 670.0,
             "right": 562.0, "size": 14.0, "bold": False},
            {"text": "freely upload, organize, and share digital", "x": 289.0, "y": 650.0,
             "right": 555.0, "size": 14.0, "bold": False},
        ]
        paragraphs = main.group_pdf_paragraphs(lines)

        self.assertEqual([len(p["lines"]) for p in paragraphs], [1, 2])
        self.assertEqual(paragraphs[0]["text"], "Digital Documents-All In One Place")

        # A bold lead-in does not split a sentence: a line's weight is the face most of its own
        # characters use, so the continuation lines stay with it.
        run_on = [
            {"text": "Note: this applies to every account", "x": 65.0, "y": 700.0,
             "right": 400.0, "size": 11.0, "bold": False},
            {"text": "created after the migration.", "x": 65.0, "y": 686.0, "right": 300.0,
             "size": 11.0, "bold": False},
        ]
        self.assertEqual(len(main.group_pdf_paragraphs(run_on)), 1)

    def test_group_pdf_paragraphs_reconnects_two_interleaved_columns(self):
        # Two_Column_Paper page 1 (real coordinates, rounded): a justified two-column layout
        # whose two columns share baselines often enough that group_pdf_lines' cell splitting (see
        # PDF_COLUMN_MIN_RUNS) hands group_pdf_paragraphs a flat list that alternates line by
        # line, right/left/right/left. Comparing only against paragraphs[-1] then means a
        # paragraph's own next line is never the thing being compared against - it is always the
        # other column - and every line came out as a paragraph of its own.
        def line(text, x, y, right):
            return {"text": text, "x": x, "y": y, "right": right, "size": 9.4}

        lines = [
            line("into the turbine blade design that", 306.64, 700.0, 543.28),
            line("The ocean floor survey revealed", 52.0, 694.0, 288.64),
            line("captures both tidal and current energy", 306.64, 688.4, 543.28),
            line("unusual sediment patterns near the", 52.0, 682.4, 288.64),
            line("across a wide range of flow speeds.", 306.64, 676.8, 460.0),
            line("coastal shelf that required further study.", 52.0, 670.8, 200.0),
        ]

        paragraphs = main.group_pdf_paragraphs(lines)

        self.assertEqual([p["text"] for p in paragraphs], [
            "into the turbine blade design that captures both tidal and current energy "
            "across a wide range of flow speeds.",
            "The ocean floor survey revealed unusual sediment patterns near the "
            "coastal shelf that required further study.",
        ])

    def test_group_pdf_paragraphs_keeps_ragged_list_rows_apart(self):
        # Powerupall page 72 (real coordinates, rounded): a two-column list of short, ragged-right
        # entries, not a justified block. The row-to-row gap here (13.56pt) is no wider than an
        # ordinary within-entry line gap - it is only smaller because the neighbouring row's own
        # first line happened to be taller - so a fix that reconnects columns by gap and alignment
        # alone would splice one entry's answer onto the next question's. Only the column's own
        # right edges - identical, page-filling, for the paper; a different length almost every
        # time here - tell the two cases apart. See PDF_LAYOUT_CONTINUATION_MIN_WIDTH.
        def line(text, x, y, right):
            return {"text": text, "x": x, "y": y, "right": right, "size": 11.04}

        lines = [
            line("I will never be able to learn this.", 95.42, 477.67, 268.16),
            line("I can try. If I take it one step at a time,", 311.45, 477.67, 516.63),
            line("it might begin to make sense.", 311.45, 464.71, 469.90),
            line("No one at the party will talk to me.", 95.42, 451.15, 281.97),
            line("I can say hello to people and see what", 311.45, 451.15, 520.01),
            line("happens. There are friendly people", 311.45, 438.19, 520.05),
            line("everywhere.", 311.45, 425.23, 375.20),
        ]

        paragraphs = main.group_pdf_paragraphs(lines)

        self.assertEqual([p["text"] for p in paragraphs], [
            "I will never be able to learn this.",
            "I can try. If I take it one step at a time, it might begin to make sense.",
            "No one at the party will talk to me.",
            "I can say hello to people and see what happens. There are friendly people "
            "everywhere.",
        ])

    def test_group_pdf_paragraphs_reunites_a_wrapped_bordered_table_header_cell(self):
        # Landscape_Mixed_Pages page 1 (real coordinates, rounded): a table header cell wrapped
        # to two lines ("Peak spring" / "velocity (m/s)"), interrupted by the rest of its own row
        # in reading order before its own second line is reached. Needs both a drawn rule near
        # its own column (has_cell_border) and a row that is only partially filled at that height
        # (row_is_partial, only 2 of 10 columns) - neither alone is safe, see the comment on the
        # last_by_x loop.
        def line(text, x, y, right, size=8.5):
            return {"text": text, "x": x, "y": y, "right": right, "size": size}

        lines = [
            line("Site", 46, 522, 65), line("Country", 122, 522, 153),
            line("Channel", 198, 522, 229), line("Peak spring", 275, 522, 317.8),
            line("Mean depth (m)", 351, 522, 409.3), line("Installed capacity", 427, 522, 491),
            line("Turbines", 503, 522, 536), line("Commissioned", 579, 522, 633),
            line("Operator", 656, 522, 690), line("Status", 732, 522, 760),
            line("velocity (m/s)", 275, 511, 323.0),
        ]
        # page_column_walls only recognises a column once several lines start at its x - three
        # data rows below the header, reusing the same ten x's, so the header's own columns
        # actually get established.
        for row, y in enumerate((495.0, 478.0, 461.0)):
            for x in (46, 122, 198, 275, 351, 427, 503, 579, 656, 732):
                lines.append(line(f"row{row}", x, y, x + 30))
        rules = [{"x": x, "bottom": 353.3, "top": 533.3}
                for x in (40, 116, 192, 268.6, 344.8, 420.9, 497.1, 573.3, 649.5, 725.7, 802)]

        paragraphs = main.group_pdf_paragraphs(lines, rules)

        self.assertIn("Peak spring velocity (m/s)", [p["text"] for p in paragraphs])

    def test_group_pdf_paragraphs_keeps_a_bordered_tables_own_rows_apart(self):
        # Table_Across_Pages (real coordinates, rounded): a genuine data table whose own
        # row-to-row gap is just as tight as a wrapped header cell's two lines, and every column
        # is filled on every row - has_cell_border alone would merge "MP-001" straight into
        # "MP-002", one whole column into a single paragraph.
        def line(text, x, y, right, size=9.0):
            return {"text": text, "x": x, "y": y, "right": right, "size": size}

        columns = (51, 121, 186, 261, 316, 386)
        rules = [{"x": x, "bottom": 82, "top": 768} for x in (40, 90, 150, 210, 280, 350, 420)]
        lines = []
        for row, y in enumerate((626.9, 610.4)):
            for x in columns:
                lines.append(line(f"row{row}-col{x}", x, y, x + 40))

        paragraphs = main.group_pdf_paragraphs(lines, rules)

        self.assertEqual(len(paragraphs), len(columns) * 2)

    def test_group_pdf_paragraphs_keeps_an_unbordered_checklists_rows_apart(self):
        # Powerupall page 85 (real coordinates, rounded): an informal two-column checklist of
        # eight independent entries, no drawn table grid at all. row_is_partial alone would merge
        # "Social status"/"Wealth"/"Attractiveness"/"Popularity" into one paragraph, because
        # page_column_walls does not reliably find every real column on a page without rules to
        # confirm them - has_cell_border (no rules here) is what keeps this one safe.
        def line(text, x, y, right, size=10.0):
            return {"text": text, "x": x, "y": y, "right": right, "size": size}

        lines = [
            line("Social status", 101, 614.6, 160), line("Career success", 317, 614.6, 380),
            line("Wealth", 101, 601.7, 140), line("Talent and skillfulness", 317, 601.7, 420),
            line("Attractiveness", 101, 588.7, 170),
            line("Receiving praise from authority figures", 317, 588.7, 490),
            line("Popularity", 101, 575.6, 150), line("Closest to perfection", 317, 575.6, 400),
        ]

        paragraphs = main.group_pdf_paragraphs(lines)

        self.assertEqual([p["text"] for p in paragraphs], [
            "Social status", "Career success", "Wealth", "Talent and skillfulness",
            "Attractiveness", "Receiving praise from authority figures", "Popularity",
            "Closest to perfection",
        ])

    def test_paragraph_center_finds_a_centred_heading(self):
        # Powerupall's text column runs 65..551. A centred heading sits within a couple of points
        # of its middle with matching room on both sides; reflow_paragraph used to set it flush
        # left, which moved the title of every chapter page.
        heading = {"lines": [{"text": "Chapter 1", "x": 265.0, "y": 700.0, "right": 347.0,
                              "size": 16.0}]}
        self.assertAlmostEqual(main.paragraph_center(heading, 65.0, 551.0), 306.0)

        # Two lines of differing width around one axis, a centred epigraph.
        quote = {"lines": [
            {"text": "Happiness depends, as Nature shows,", "x": 190.0, "y": 700.0,
             "right": 422.0, "size": 11.0},
            {"text": "less on exterior things", "x": 232.0, "y": 686.0, "right": 380.0,
             "size": 11.0},
        ]}
        self.assertAlmostEqual(main.paragraph_center(quote, 65.0, 551.0), 306.0)

    def test_paragraph_center_leaves_flowed_and_indented_text_alone(self):
        # Justified body text fills the column, so every line shares its centre. Without the
        # clearance test that read as centred (Powerupall, "5. The goal is to slowly become...").
        body = {"lines": [
            {"text": "5. The goal is to slowly become more conditioned,", "x": 47.0, "y": 700.0,
             "right": 550.0, "size": 11.0},
            {"text": "and lung capacity. A well-conditioned body performs", "x": 65.0,
             "y": 686.0, "right": 551.0, "size": 11.0},
        ]}
        self.assertIsNone(main.paragraph_center(body, 65.0, 551.0))

        # An indented block is set against its own left edge, not around a centre.
        indented = {"lines": [
            {"text": "the first line of the quotation runs long", "x": 150.0, "y": 700.0,
             "right": 540.0, "size": 11.0},
            {"text": "and the second is shorter", "x": 150.0, "y": 686.0, "right": 300.0,
             "size": 11.0},
        ]}
        self.assertIsNone(main.paragraph_center(indented, 65.0, 551.0))

    def test_paragraph_center_measures_against_the_cell_a_paragraph_sits_in(self):
        # Systemrequirements' "Minimum" column header is flush left in its own cell and only
        # happens to land near the middle of the page.
        header = {"lines": [{"text": "Minimum", "x": 234.0, "y": 700.0, "right": 270.0,
                             "size": 8.5}]}
        cell = {"x": 225.0, "right": 362.0, "top": 712.0, "bottom": 694.0}

        self.assertIsNone(main.paragraph_center(header, 54.0, 453.0, [cell]))
        # Without the cell, the page margins alone make it look centred.
        self.assertIsNotNone(main.paragraph_center(header, 54.0, 453.0))

        # A full-page background is not a column: measured against it, ordinary body text sits
        # symmetrically and read as centred (Geschäftsbedingungen).
        body = {"lines": [{"text": "Unsere Angebote haben eine Gültigkeitsdauer", "x": 71.0,
                           "y": 700.0, "right": 527.0, "size": 11.0}]}
        page_background = {"x": 0.0, "right": 594.0, "top": 800.0, "bottom": 0.0}

        self.assertIsNone(main.paragraph_center(body, 71.0, 539.0, [page_background]))

    def test_reflow_paragraph_sets_a_centred_paragraph_around_its_axis(self):
        heading = {"lines": [{"text": "Chapter 1", "x": 265.0, "y": 700.0, "right": 347.0,
                              "size": 16.0}]}
        placed = main.reflow_paragraph(heading, "Kapitel 1", width_limit=551.0, centre=306.0)

        self.assertEqual(len(placed), 1)
        width = main.pdf_measure_text(placed[0]["text"], placed[0]["size"], False, False)
        self.assertAlmostEqual(placed[0]["x"] + width / 2, 306.0, places=3)
        # A centred paragraph is never also justified, that would undo the centring line by line.
        self.assertNotIn("justify_to", placed[0])

    def test_paragraph_width_limit_ignores_an_icon_it_cannot_fit_inside(self):
        # Get_Started_With_Smallpdf draws 139 icons and decorations. One that merely started left
        # of a line's own x claimed to be that line's box: a 42pt icon walled a 253pt paragraph in
        # at 19pt, which reflowed to one word per line, blew past PDF_LAYOUT_MAX_LINE_GROWTH and
        # left three of the four body paragraphs in English.
        paragraph = {"lines": [
            {"text": "Digital Documents-All In One Place", "x": 289.0, "y": 694.0,
             "right": 508.0, "size": 14.0},
            {"text": "With the new Smallpdf experience, you can", "x": 289.0, "y": 676.0,
             "right": 562.0, "size": 14.0},
        ]}
        icon = {"x": 268.0, "right": 310.0, "top": 717.0, "bottom": 652.0}

        self.assertEqual(main.paragraph_width_limit(paragraph, [paragraph], 560.0, [icon]), 562.0)

    def test_paragraph_floor_stops_above_an_image_and_inside_a_box(self):
        paragraph = {"lines": [{"text": "text", "x": 50.0, "y": 700.0, "right": 300.0, "size": 10.0}]}
        image = {"x": 40.0, "right": 320.0, "top": 660.0, "bottom": 500.0}
        box = {"x": 40.0, "right": 320.0, "top": 720.0, "bottom": 680.0}

        self.assertEqual(main.paragraph_floor(paragraph, [paragraph], [image]), 660.0)
        # Sitting inside a box, the paragraph may grow down to that box's lower edge.
        self.assertEqual(main.paragraph_floor(paragraph, [paragraph], [box]), 680.0)

    def test_reflow_paragraph_uses_the_full_height_of_a_roomy_box(self):
        # A box's lower edge is an edge, not the baseline of a next paragraph: only the descenders
        # of the last line have to stay above it. Demanding a full em there cost a cell with room
        # to spare a line, and with it type size.
        paragraph = {"lines": [
            {"text": "one line", "x": 50.0, "y": 700.0, "right": 200.0, "size": 10.0},
        ]}
        text = "Eine Uebersetzung die zwei Zeilen braucht"
        # A second line lands on 688, and the box ends at 685: room for its descenders, not for a
        # whole line of clearance.
        in_box = main.reflow_paragraph(paragraph, text, floor=685.0, box_floor=685.0)
        below_it = main.reflow_paragraph(paragraph, text, floor=685.0)

        self.assertEqual(len(in_box), 2)
        self.assertGreaterEqual(in_box[0]["size"], 10.0 * main.PDF_LAYOUT_TIGHTEN_SCALE)
        # The same floor as another paragraph's baseline keeps the full em, and pays for it.
        self.assertLess(below_it[0]["size"], in_box[0]["size"])

    def test_reflow_paragraph_shrinks_harder_to_stay_inside_a_flat_cell(self):
        # A flat table cell leaves nothing below its baseline, so an extra line does not crowd
        # the next paragraph, it stands outside the table (Systemrequirements, "Minimum" column).
        # There the translation is worth more type size than elsewhere.
        line = {"text": "RAM", "x": 143.0, "y": 549.7, "right": 240.0, "size": 8.5}
        paragraph = {"lines": [dict(line)]}
        cell = 547.5
        text = "Arbeitsspeicher mit reichlich Reserve"

        boxed = main.reflow_paragraph(paragraph, text, floor=cell, width_limit=240.0,
                                      box_floor=cell)
        open_below = main.reflow_paragraph(paragraph, text, floor=cell, width_limit=240.0)

        # Inside the cell: one line, down to whatever size that took.
        self.assertEqual(len(boxed), 1)
        self.assertLess(boxed[0]["size"], 8.5 * main.PDF_LAYOUT_MIN_SCALE)
        self.assertGreaterEqual(boxed[0]["size"], 8.5 * main.PDF_LAYOUT_BOXED_MIN_SCALE)
        # The same floor with a paragraph below rather than a cell edge also stays on one line,
        # and pays more type size for it: clearing another baseline takes a full em where clearing
        # an edge takes only the descenders.
        self.assertEqual(len(open_below), 1)
        self.assertLess(open_below[0]["size"], boxed[0]["size"])
        self.assertGreaterEqual(open_below[0]["size"], 8.5 * main.PDF_LAYOUT_CROWDED_MIN_SCALE)

    def test_enclosing_box_bottom_ignores_a_box_the_paragraph_is_not_in(self):
        paragraph = {"lines": [{"text": "cell", "x": 50.0, "y": 700.0, "right": 150.0, "size": 10.0}]}
        cell = {"x": 40.0, "right": 160.0, "top": 712.0, "bottom": 697.0}
        beside = {"x": 200.0, "right": 300.0, "top": 712.0, "bottom": 697.0}
        below = {"x": 40.0, "right": 160.0, "top": 660.0, "bottom": 600.0}

        self.assertEqual(main.enclosing_box_bottom(paragraph, [cell, beside, below]), 697.0)
        self.assertIsNone(main.enclosing_box_bottom(paragraph, [beside, below]))

    def test_paragraph_floor_ignores_a_shape_on_the_paragraphs_own_last_line(self):
        # A shape belonging to the last line itself - a marker, a small icon - sits a fraction of
        # an em below its baseline. Taken as a floor it would stop the paragraph growing at all.
        # (A box that encloses the line is a different matter: a table cell has to stop it.)
        paragraph = {"lines": [{"text": "text", "x": 50.0, "y": 700.0, "right": 300.0, "size": 10.0}]}
        icon = {"x": 200.0, "right": 250.0, "top": 698.0, "bottom": 690.0}

        self.assertEqual(main.paragraph_floor(paragraph, [paragraph], [icon]),
                         main.PDF_LAYOUT_EDGE_MARGIN)

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
        # Wrapping is against the document's own right margin, so the line may break anywhere.
        self.assertIn("Second page translated", " ".join(pages_text[0].split()))
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
            old_time = time.time() - (main.HISTORY_HOURS * 3600) - 60
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

    def test_delete_history_removes_cached_original_format_export(self):
        # history_original_export writes this lazily on first original-format download and
        # caches it (history_export_path) - delete_history used to only clean up the source
        # file, leaving this one behind until the time-based cleanup_history caught up with it.
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "HISTORY_DIR", temp_dir):
                item_id = main.save_history(
                    "text", "Hallo", "eng_Latn", "deu_Latn", "source.docx",
                    minimal_docx(["Hello"]), "docx",
                )
                export_path = main.history_export_path(item_id, "docx")
                export_path.write_bytes(b"cached export")
                response = TestClient(main.app).delete(f"/history/{item_id}")

            self.assertEqual(response.status_code, 200)
            self.assertFalse(export_path.exists())
        finally:
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_reset_history_keeps_only_the_newest_entries(self):
        temp_dir = test_temp_dir()
        try:
            for index, day in enumerate(("01", "02", "03")):
                item_id = f"2026-08-{day}-text-{index}"
                (temp_dir / f"{item_id}.md").write_text("result", encoding="utf-8")
                (temp_dir / f"{item_id}.json").write_text(json.dumps({
                    "id": item_id, "kind": "text", "source": "eng_Latn", "target": "deu_Latn",
                    "created_at": f"2026-08-{day}T00:00:00+00:00", "filename": f"{item_id}.md",
                }), encoding="utf-8")

            with patch.object(main, "HISTORY_DIR", temp_dir):
                response = TestClient(main.app).delete("/history?keep=1")
                remaining = main.history_items()

            self.assertEqual(response.json(), {"deleted": 2, "kept": 1})
            self.assertEqual([item["id"] for item in remaining], ["2026-08-03-text-2"])
        finally:
            shutil.rmtree(temp_dir.parent, ignore_errors=True)

    def test_update_job_caps_percent_below_complete_while_still_running(self):
        temp_dir = test_temp_dir()
        try:
            with patch.object(main, "JOBS_DIR", temp_dir):
                with main.JOBS_LOCK:
                    main.JOBS.clear()
                    main.JOB_RUNNERS.clear()
                job_id = main.create_job("translate-pdf-layout", "eng_Latn", "deu_Latn", "big.pdf")
                main.update_job(job_id, status="running", total=10, current=10, message="Rendering PDF")
                # Every chunk translated, but the job is still running (PDF rendering/export
                # comes after) - showing 100% here would read as finished when it is not.
                self.assertEqual(main.get_job(job_id)["percent"], 99.0)
                self.assertEqual(main.get_job(job_id)["eta_seconds"], 0)

                main.update_job(job_id, status="complete", message="Complete")

                self.assertEqual(main.get_job(job_id)["percent"], 100.0)
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

        # One block per cell, not one block per row joined with " | " - the model doesn't
        # reliably keep a literal separator character through translation (measured: dropped
        # entirely), which silently misaligned every column after the first.
        self.assertEqual(text, "Hello\n\nWorld\n\nSecond\n\nRow")

    def test_csv_export_replaces_selected_columns(self):
        content = b"title,description,ignore\nHello,World,Nope\nSecond,Row,Skip\n"

        updated = main.export_csv_with_translated_text(content, "title, description", "Hallo\n\nWelt\n\nZweite\n\nZeile")

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

        # One block per cell - see the CSV version of this test for why.
        self.assertEqual(text, "Hello\n\nWorld")

    def test_xlsx_export_replaces_selected_columns(self):
        updated = main.export_xlsx_with_translated_text(minimal_xlsx(), "Sheet1", "title,description", "Hallo\n\nWelt")

        text = main.extract_xlsx_text_from_bytes(updated, "Sheet1", "title,description")
        self.assertEqual(text, "Hallo\n\nWelt")

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
