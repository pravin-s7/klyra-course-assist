"""Persist extracted diagrams and let the QP Creation Agent find/embed them.

Captions come from the "[Figure: ...]" descriptions the Document Parsing
Agent's Gemini call already produces inline in the normalized text (see
gemini_client.py's prompt) - reusing that means indexing images costs zero
extra Gemini calls.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.tools import vector_store
from klyra.tools.image_extraction import ExtractedImage
from klyra.utils import fs

_FIGURE_RE = re.compile(r"\[Figure:([^\]]*)\]")


def _figure_captions(text: str) -> list[str]:
    return [m.group(1).strip() for m in _FIGURE_RE.finditer(text) if m.group(1).strip()]


def save_and_index_images(
    subject: str,
    source_file: str,
    week: int | None,
    images: list[ExtractedImage],
    parsed_text: str,
) -> int:
    """Writes extracted images to docs/<subject>/.assets/<source-slug>/ and
    indexes them (captioned, searchable) into the <subject>-images
    collection. Returns how many were saved."""
    if not images:
        return 0

    captions = _figure_captions(parsed_text)
    caption_text = " ".join(captions) if captions else f"Diagram from {Path(source_file).name}"

    out_dir = fs.assets_dir(subject) / fs.slugify(Path(source_file).stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    for idx, image in enumerate(images):
        image_path = out_dir / f"p{image.page}-{idx}.{image.ext}"
        image_path.write_bytes(image.data)
        chunks.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_file}::img::{idx}")),
                "text": f"{caption_text} (page {image.page})",
                "metadata": {
                    "subject": subject,
                    "week": week or 0,
                    "source_file": source_file,
                    "page": image.page,
                    "image_path": fs.markdown_image_path(subject, image_path),
                },
            }
        )
    vector_store.add_chunks(vector_store.images_collection_name(subject), chunks)
    return len(chunks)


class SearchImagesInput(BaseModel):
    subject: str
    query: str = Field(..., description="What kind of diagram/figure you need, e.g. 'B-tree insertion'")
    week: int | None = Field(default=None, description="Restrict to a specific week (1-12)")
    top_k: int = Field(default=3)


class SearchImagesTool(BaseTool):
    name: str = "search_images"
    description: str = (
        "Search for a real extracted diagram/figure (graph, tree, B-tree, ER "
        "diagram, architecture diagram, etc.) from the subject's source "
        "material. Returns ready-to-paste markdown image syntax. Only embed "
        "a result in a question if it's actually relevant to that question - "
        "never fabricate a diagram reference when no good match exists; "
        "write the question as text-only instead."
    )
    args_schema: Type[BaseModel] = SearchImagesInput

    def _run(self, subject: str, query: str, week: int | None = None, top_k: int = 3) -> str:
        where = {"week": week} if week is not None else None
        matches = vector_store.query(
            vector_store.images_collection_name(subject), query, n_results=top_k, where=where
        )
        if not matches:
            return "No matching diagrams found - write this question as text-only."
        return "\n".join(
            f"- ![{m['text'][:70]}]({m['metadata'].get('image_path')}) "
            f"| week {m['metadata'].get('week')}, source: {m['metadata'].get('source_file')}, "
            f"page {m['metadata'].get('page')}, distance={m['distance']:.3f}"
            for m in matches
        )
