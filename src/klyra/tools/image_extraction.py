"""Extracts meaningful (non-template) images from PDF bytes so the QP
Creation Agent can embed real diagrams (graphs, trees, B-trees, ER diagrams,
architecture diagrams, ...) in generated questions instead of only
describing them in text.

Pure local pypdf/Pillow work - no extra Gemini calls, so it doesn't touch
the LLM quota budget documented in .cursor/rules/klyra.mdc.

Works on native PDFs and on ppt/doc files already converted to PDF via
LibreOffice (document_parser_tool passes the same pdf_bytes to both this and
the Gemini multimodal call).

Filtering: real lecture slides/PDFs almost always repeat a header/footer
banner or logo image on every single page - that's noise, not content.
Confirmed empirically on a real lecture PDF: a 26-page deck had one image
repeated on all 26 pages (a template banner) and 6 genuinely unique diagrams
that each appeared exactly once. We drop anything above
MAX_PAGE_OCCURRENCES and anything smaller than MIN_DIMENSION (bullet
icons/decorative marks).
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

MIN_DIMENSION = 80
MAX_PAGE_OCCURRENCES = 2


@dataclass(frozen=True)
class ExtractedImage:
    page: int
    data: bytes
    ext: str
    width: int
    height: int


def extract_images_from_pdf(pdf_bytes: bytes) -> list[ExtractedImage]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    first_seen: dict[str, ExtractedImage] = {}
    pages_seen: dict[str, set[int]] = {}

    for page_num, page in enumerate(reader.pages, start=1):
        try:
            page_images = list(page.images)
        except Exception:  # noqa: BLE001 - one malformed image shouldn't kill parsing
            continue

        for img in page_images:
            digest = hashlib.sha256(img.data).hexdigest()
            pages_seen.setdefault(digest, set()).add(page_num)
            if digest in first_seen:
                continue
            try:
                from PIL import Image

                with Image.open(io.BytesIO(img.data)) as pil_img:
                    width, height = pil_img.size
                    fmt = (pil_img.format or "PNG").lower()
            except Exception:  # noqa: BLE001 - skip images Pillow can't decode
                continue
            if width < MIN_DIMENSION or height < MIN_DIMENSION:
                continue
            ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
            first_seen[digest] = ExtractedImage(page=page_num, data=img.data, ext=ext, width=width, height=height)

    return [image for digest, image in first_seen.items() if len(pages_seen[digest]) <= MAX_PAGE_OCCURRENCES]
