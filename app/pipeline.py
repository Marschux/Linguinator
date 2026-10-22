"""The job runners: plain text and files, the Markdown PDF path and the layout PDF path."""
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from fastapi import HTTPException
from . import config, langs, translation, history, jobqueue, pdf_text, pdf_layout, pdf_scan


def run_text_job(
    job_id: str,
    text: str,
    source: str,
    target: str,
    original_name: str = "text",
    source_content: bytes = b"",
    source_extension: str = "",
    source_meta: Optional[Dict[str, str]] = None,
):
    try:
        mixed = source == langs.AUTO_SOURCE
        if mixed:
            source = langs.detect_source_language(text)
            jobqueue.update_job(job_id, source=source)
        jobqueue.update_job(job_id, status="running", message="Model loading or translation running", started_at=time.time())
        if source_extension == "md":
            # Headings/lists/quotes need their markers kept out of what the model sees - see
            # translate_markdown_document.
            result, chunk_count = translation.translate_markdown_document(text, source, target, job_id, mixed)
        else:
            # One paragraph per translate_batch entry: translate_one/translate_batch rejoin
            # sentences with a single space, so a multi-paragraph text sent as one piece loses
            # its blank lines. The original-format exports map translated_blocks() back by
            # splitting on those lines, and a 6-paragraph DOCX put everything into paragraph 1.
            paragraphs = [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()] or [text]
            jobqueue.update_job(job_id, total=len(paragraphs), message=f"Translating 0 / {len(paragraphs)} paragraphs")
            result = "\n\n".join(translation.translate_chunks_batched(paragraphs, source, target, job_id, config.PDF_LAYOUT_BATCH_SIZE, mixed))
            chunk_count = len(paragraphs)
        history_id = history.save_history(
            "text",
            result,
            source,
            target,
            original_name,
            source_content,
            source_extension,
            source_meta,
        )
        jobqueue.update_job(
            job_id,
            status="complete",
            message="Complete",
            current=chunk_count,
            result=result,
            history_id=history_id,
            finished_at=time.time(),
        )
        jobqueue.cleanup_job_payload(job_id)
    except Exception as exc:
        if str(exc) == "Job stopped by user":
            jobqueue.update_job(job_id, status="cancelled", message="Cancelled", error=None, finished_at=time.time())
        else:
            jobqueue.update_job(job_id, status="failed", message="Failed", error=jobqueue.exception_message(exc), finished_at=time.time())
        jobqueue.cleanup_job_payload(job_id)


def pdf_sections(markdown: str):
    parts = re.split(r"(?m)^# Page ", markdown)
    if len(parts) <= 1:
        # No "# Page N" marker anywhere, e.g. any non-PDF-sourced translation: re.split still
        # returns the whole text as one part, which used to get its first line torn off and
        # misread as a page number/heading instead of being treated as plain body text.
        return []
    sections = []
    for section in parts:
        section = section.strip()
        if not section:
            continue
        page_number, _, page_text = section.partition("\n")
        sections.append((page_number.strip(), page_text.strip()))
    return sections


def merge_cross_page_sentences(sections: List[Tuple[str, str]],
                               noise: Optional[Set[str]] = None) -> List[Tuple[str, str]]:
    """Move a page's trailing incomplete sentence onto the next page's text.

    Pages are joined with a hard break regardless of where a sentence actually ends, so a
    sentence that runs across a page boundary in the source PDF is cut into two fragments -
    each translated on its own, neither a complete sentence. Measured on a real OCR page: the
    tail end of one such fragment came back as an invented sentence, the other as a plausible
    translation rejected by guard_hallucination for running slightly long, because neither half
    read as the sentence it actually was. Carrying the trailing fragment forward before chunking
    keeps the sentence whole for translation; the page marker it ends up under shifts by one
    sentence, no worse than the original PDF already showing the same split across the same two
    pages.
    """
    sections = list(sections)
    for index in range(len(sections) - 1):
        number, text = sections[index]
        # A score page is planned line by line (plan_pdf_page), a carried line would land in the
        # middle of the next one's.
        if not text or pdf_scan.is_score_page(text, noise) or pdf_scan.is_score_page(sections[index + 1][1], noise):
            continue
        matches = list(translation.SENTENCE_END.finditer(text))
        cut = matches[-1].end() if matches else 0
        if cut == 0 or cut >= len(text):
            continue
        carry = text[cut:].strip()
        if not carry:
            continue
        next_number, next_text = sections[index + 1]
        sections[index] = (number, text[:cut].strip())
        sections[index + 1] = (next_number, (carry + " " + next_text).strip())
    return sections


def run_pdf_translate_job(
    job_id: str,
    content: bytes,
    content_type: str,
    source: str,
    target: str,
    filename: str,
    page_range: str = "",
    layout_fallback: bool = False,
):
    try:
        # A scanned page with no source language set is read twice, which takes noticeably longer,
        # so the message says why instead of letting the job look stuck.
        extracting = "Extracting PDF (detecting scan language)" if source == langs.AUTO_SOURCE else "Extracting PDF"
        jobqueue.update_job(job_id, status="running", message=extracting, started_at=time.time())
        ocr_pages: Dict[int, List[Dict[str, Any]]] = {}
        markdown = pdf_text.extract_pdf_markdown_from_bytes(content, content_type, page_range, source, ocr_pages)
        noise = {line["text"] for lines in ocr_pages.values() for line in lines if line["noise"]}
        requested_source = source
        mixed = source == langs.AUTO_SOURCE
        if mixed:
            source = langs.detect_source_language(markdown)
            jobqueue.update_job(job_id, source=source)
        if layout_fallback and any(ocr_pages.values()):
            # Asked for the original layout: set the translation onto the scanned pages themselves.
            pages = pdf_scan.scan_layout_pages(content, ocr_pages, page_range, source)
            paragraphs = [paragraph["text"] for page in pages for paragraph in page["paragraphs"]]
            # A score's lines go to the model as whole sentences and come back spread over them.
            groups = pdf_scan.sung_sentences(pages)
            jobqueue.update_job(job_id, total=len(groups), message=f"Translating 0 / {len(groups)} chunks")
            joined = translation.translate_chunks([" ".join(paragraphs[i] for i in group) for group in groups],
                                                  source, target, job_id, mixed)
            translated = list(paragraphs)
            for group, text in zip(groups, joined):
                for position, part in zip(group, pdf_scan.spread_words(text, [paragraphs[i] for i in group])):
                    translated[position] = part
            # One line per paragraph, so the re-export's split on blank lines stays exact.
            result = "\n\n".join(re.sub(r"\s+", " ", text).strip() for text in translated)
            meta = {"layout": "true", "scan": "true", "page_range": page_range, "scan_source": requested_source,
                    "scan_language": source}
            history_id = history.save_history("translate-pdf-layout", result, source, target, filename, content, "pdf", meta)
            jobqueue.update_job(job_id, message="Rendering PDF")
            try:
                history.history_original_export(history_id, "pdf", content, result, meta)
            except Exception:
                # Same as the layout job: the translation is saved, the download builds it again.
                pass
            jobqueue.update_job(job_id, status="complete", message="Complete", current=len(groups),
                       result=result, history_id=history_id, finished_at=time.time())
            jobqueue.cleanup_job_payload(job_id)
            return
        markdown = translation.expand_lowercase_ligatures(markdown, source)
        sections = merge_cross_page_sentences(pdf_sections(markdown), noise)
        planned = [(page_number, pdf_scan.plan_pdf_page(page_text, noise)) for page_number, page_text in sections]
        chunks = [piece for _, pieces in planned for piece, translate in pieces if translate]
        total = len(chunks)
        jobqueue.update_job(job_id, total=total, message=f"Translating 0 / {total} chunks")

        # All pages in one call, so a mixed document is grouped by language across pages, not per page.
        translated = iter(translation.translate_chunks(chunks, source, target, job_id, mixed))
        pages = [f"# Page {page_number}\n\n" + "\n\n".join(next(translated) if translate else piece
                                                          for piece, translate in pieces)
                 for page_number, pieces in planned]

        result = "\n\n".join(pages)
        history_id = history.save_history("pdf", result, source, target, filename, content, "pdf")
        complete_message = (
            "Complete (no positioned text on the selected pages, used plain export instead of layout)"
            if layout_fallback else "Complete"
        )
        jobqueue.update_job(
            job_id,
            status="complete",
            message=complete_message,
            current=total,
            result=result,
            history_id=history_id,
            finished_at=time.time(),
        )
        jobqueue.cleanup_job_payload(job_id)
    except Exception as exc:
        if str(exc) == "Job stopped by user":
            jobqueue.update_job(job_id, status="cancelled", message="Cancelled", error=None, finished_at=time.time())
        else:
            jobqueue.update_job(job_id, status="failed", message="Failed", error=jobqueue.exception_message(exc), finished_at=time.time())
        jobqueue.cleanup_job_payload(job_id)


def run_pdf_layout_translate_job(
    job_id: str,
    content: bytes,
    source: str,
    target: str,
    filename: str,
    page_range: str = "",
):
    try:
        jobqueue.update_job(job_id, status="running", message="Reading PDF layout", started_at=time.time())
        try:
            pages = pdf_layout.extract_pdf_layout(content, page_range)
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            # Scanned/image-only PDF: no positioned text to lay a translation back over, so this
            # falls back to the plain extract-then-translate pipeline, which has an OCR fallback.
            run_pdf_translate_job(job_id, content, "application/pdf", source, target, filename, page_range, layout_fallback=True)
            return
        jobqueue.wait_if_paused_or_cancelled(job_id)
        # Widget field values are interleaved after their page's ordinary paragraphs, not
        # collected separately: the stored translated_text is one flat "\n\n"-joined sequence
        # matched to extract_pdf_layout's output purely by position (history re-export re-runs
        # that same extraction and zips the blocks back on), so a field's translation has to sit
        # at the exact position render_pdf_layout_overlay will look for it at, see there.
        paragraphs = [item["text"] for page in pages
                      for item in page["paragraphs"] + page["widgets"]]
        mixed = source == langs.AUTO_SOURCE
        if mixed:
            source = langs.detect_source_language("\n\n".join(paragraphs))
            jobqueue.update_job(job_id, source=source)
        # Now that the language is settled, see expand_lowercase_ligatures. Only the text handed
        # to the model is repaired; placement runs off the lines' coordinates, and a paragraph
        # count that stayed the same keeps the re-export lined up.
        paragraphs = [translation.expand_lowercase_ligatures(text, source) for text in paragraphs]
        # One paragraph per chunk: the overlay maps translations back to paragraphs by position,
        # and the model does not reliably keep paragraph breaks inside a single chunk. Several
        # chunks are still translated per model call (translate_chunks_batched), via the tensor's
        # batch dimension rather than concatenated text, so this mapping stays exact.
        chunks: List[str] = []
        chunk_counts: List[int] = []
        for paragraph in paragraphs:
            # Fragments with no word in them keep their original: see has_translatable_text.
            parts = translation.split_long_text(paragraph, config.MAX_CHARS) if pdf_layout.has_translatable_text(paragraph) else []
            chunks.extend(parts)
            chunk_counts.append(len(parts))
        jobqueue.update_job(job_id, total=len(chunks), message=f"Translating 0 / {len(chunks)} chunks")
        translated_chunks = translation.translate_chunks_batched(chunks, source, target, job_id, config.PDF_LAYOUT_BATCH_SIZE, mixed)

        translated: List[str] = []
        position = 0
        for paragraph, count in zip(paragraphs, chunk_counts):
            if not count:
                # Untranslated fragment: keep the original so the paragraph still has an entry and
                # every translation after it stays on its own paragraph.
                translated.append(re.sub(r"\s+", " ", paragraph).strip())
                continue
            # Collapsed to a single line so the paragraph split on re-export stays exact.
            translated.append(re.sub(r"\s+", " ", " ".join(translated_chunks[position:position + count])).strip())
            position += count
        result = "\n\n".join(translated)
        history_id = history.save_history(
            "translate-pdf-layout",
            result,
            source,
            target,
            filename,
            content,
            "pdf",
            {"layout": "true", "page_range": page_range},
        )
        jobqueue.update_job(job_id, message="Rendering PDF")
        try:
            history.history_original_export(history_id, "pdf", content, result,
                                    {"layout": "true", "page_range": page_range})
        except Exception:
            # The translation is done and saved; only the head start on the download is lost, and
            # the download builds it again. Failing the whole job over that would throw away work.
            pass
        jobqueue.update_job(
            job_id,
            status="complete",
            message="Complete",
            current=len(chunks),
            result=result,
            history_id=history_id,
            finished_at=time.time(),
        )
        jobqueue.cleanup_job_payload(job_id)
    except Exception as exc:
        if str(exc) == "Job stopped by user":
            jobqueue.update_job(job_id, status="cancelled", message="Cancelled", error=None, finished_at=time.time())
        else:
            jobqueue.update_job(job_id, status="failed", message="Failed", error=jobqueue.exception_message(exc), finished_at=time.time())
        jobqueue.cleanup_job_payload(job_id)
