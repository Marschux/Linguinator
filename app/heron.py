"""Docling's layout model (heron): where a page's text blocks are, as a hint for group_pdf_paragraphs."""
import logging
from functools import lru_cache
from typing import Any, List
import pymupdf
from . import config, models


# Blocks that hold running text. A table or form is one block to the model but many cells to
# us, and text inside a picture has no block of its own: those leave the geometry alone.
HERON_TEXT_LABELS = {"text", "section_header", "title", "list_item", "caption", "footnote",
                     "page_header", "page_footer", "code", "formula"}
HERON_MIN_SCORE = 0.5
HERON_DPI = 144


@lru_cache(maxsize=1)
def load_layout_model():
    from transformers import RTDetrImageProcessor, RTDetrV2ForObjectDetection
    return (RTDetrImageProcessor.from_pretrained(config.LAYOUT_MODEL_ID),
            RTDetrV2ForObjectDetection.from_pretrained(config.LAYOUT_MODEL_ID).eval())


def page_text_blocks(page) -> List[Any]:
    """The page's text blocks as rects in PDF user space (the frame pdf_page_runs puts lines in).
    Empty when the model cannot run: the geometry then decides alone."""
    models.begin_model_use()
    try:
        import torch
        from PIL import Image
        processor, model = load_layout_model()
        pix = page.get_pixmap(dpi=HERON_DPI)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        with torch.no_grad():
            output = model(**processor(images=image, return_tensors="pt"))
        found = processor.post_process_object_detection(
            output, target_sizes=torch.tensor([image.size[::-1]]), threshold=HERON_MIN_SCORE)[0]
    except Exception:
        # A missing download on an offline host must not fail the job.
        logging.getLogger(__name__).exception("Layout model unavailable, paragraphs from geometry only")
        return []
    finally:
        models.end_model_use()
    # Rendered with /Rotate applied and counting down from the top; lines are in the raw frame,
    # counting up. ponytail: assumes the crop box sits at the media box's corner, as in pdf_scan.
    scale = 72.0 / HERON_DPI
    to_user = pymupdf.Matrix(scale, scale) * page.derotation_matrix * pymupdf.Matrix(1, 0, 0, -1, 0, page.mediabox.height)
    return [pymupdf.Rect(*box.tolist()) * to_user
            for label, box in zip(found["labels"].tolist(), found["boxes"])
            if model.config.id2label[label] in HERON_TEXT_LABELS]
