"""Parses docs/<subject>/{week_*,additional,pyq,transcripts}/* into
normalized text and stores it in the vector store (klyra.tools.vector_store).

Parsing strategy per .cursor/rules/klyra.mdc:
  - .md/.txt/code files       -> read directly as text.
  - .pdf                      -> sent to Gemini (multimodal) for text +
                                  faithful descriptions of images/diagrams.
  - .ppt/.pptx/.doc/.docx     -> converted to PDF via LibreOffice, then the
                                  same Gemini path. If LibreOffice isn't
                                  installed, falls back to text-only
                                  extraction (python-pptx/python-docx) and
                                  reports the file as "degraded" (images
                                  lost). If GEMINI_API_KEY isn't set, PDFs
                                  fall back to a text-only extraction too.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.tools import vector_store
from klyra.tools.gemini_client import GeminiNotConfiguredError, describe_document
from klyra.tools.image_extraction import ExtractedImage, extract_images_from_pdf
from klyra.tools.image_tools import save_and_index_images
from klyra.utils import fs


def _chunk_text(text: str, chunk_size: int = 1500, overlap: int = 150) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def _soffice_path() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_office_to_pdf(path: Path) -> bytes | None:
    soffice = _soffice_path()
    if not soffice:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(path)],
            check=True,
            capture_output=True,
            timeout=120,
        )
        out = Path(tmp) / (path.stem + ".pdf")
        if out.exists():
            return out.read_bytes()
    return None


def _fallback_office_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pptx":
        from pptx import Presentation

        prs = Presentation(str(path))
        parts = []
        for i, slide in enumerate(prs.slides, start=1):
            parts.append(f"[Slide {i}]")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
        return "\n".join(parts)
    if suffix == ".docx":
        import docx

        document = docx.Document(str(path))
        return "\n".join(p.text for p in document.paragraphs)
    raise ValueError(
        f"No LibreOffice ('soffice') found to convert '{path.name}', and no "
        f"text-only fallback exists for '{suffix}' (.ppt/.doc need it too - "
        "only .pptx/.docx have a fallback). Install LibreOffice for full parsing."
    )


def parse_file_to_text(path: Path) -> tuple[str, bool, list[ExtractedImage]]:
    """Returns (normalized_text, degraded, images). degraded=True means
    images/diagrams in the source were not *understood* (no Gemini key, or a
    text-only fallback was used) - `images` (the actual extracted diagram
    bytes) can still be non-empty even when degraded=True, since extraction
    is local pypdf work independent of Gemini."""
    suffix = path.suffix.lower()

    if suffix in fs.PLAIN_TEXT_SUFFIXES or suffix in fs.CODE_SUFFIXES:
        return path.read_text(errors="ignore"), False, []

    if suffix in fs.IMAGE_SUFFIXES:
        img_bytes = path.read_bytes()
        try:
            from PIL import Image
            import io
            with Image.open(io.BytesIO(img_bytes)) as pil_img:
                width, height = pil_img.size
                fmt = (pil_img.format or "PNG").lower()
        except Exception:
            return "", True, []
            
        ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
        img_obj = ExtractedImage(page=1, data=img_bytes, ext=ext, width=width, height=height)
        try:
            return describe_document(img_bytes, f"image/{ext}", path.name), False, [img_obj]
        except GeminiNotConfiguredError:
            return "", True, [img_obj]

    if suffix == fs.PDF_SUFFIX:
        pdf_bytes = path.read_bytes()
    elif suffix in fs.OFFICE_SUFFIXES:
        pdf_bytes = _convert_office_to_pdf(path)
    else:
        raise ValueError(f"Unsupported file type: {path}")

    if pdf_bytes is None:
        # Office format with no LibreOffice available - text-only fallback,
        # and no PDF bytes means no local image extraction is possible either.
        return _fallback_office_text(path), True, []

    images = extract_images_from_pdf(pdf_bytes)
    try:
        return describe_document(pdf_bytes, "application/pdf", path.name), False, images
    except GeminiNotConfiguredError:
        if suffix == fs.PDF_SUFFIX:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            text = _fallback_office_text(path)
        return text, True, images


class DocumentParserInput(BaseModel):
    subject: str = Field(..., description="The subject ID (e.g. dbms, se, ml)")
    week: int | None = Field(None, description="The week number to parse. None means all weeks.")
    categories_filter: str | None = Field(None, description="Comma-separated categories to filter by (e.g. pyq,transcript)")
    file_path: str | None = Field(None, description="A specific file path to parse.")
    force: bool = Field(False, description="Whether to re-parse already ingested files.")


class DocumentParserTool(BaseTool):
    name: str = "parse_subject_documents"
    description: str = (
        "Parse every new/changed source file for a subject (pdf, ppt, pptx, "
        "doc, docx, md, code, transcripts) into normalized text, and extract "
        "real embedded diagrams/figures (graphs, trees, ER diagrams, etc. - "
        "not repeated logos/banners) for later use by search_images. PYQ "
        "files are stored directly in the '<subject>-pyq' vector collection; "
        "everything else goes into '<subject>-parsed' for the Syllabus "
        "Indexer Agent to read next. Returns a short summary, not the full "
        "text, to keep context small."
    )
    args_schema: Type[BaseModel] = DocumentParserInput

    def _run(
        self, 
        subject: str, 
        week: int | None = None, 
        categories_filter: str | None = None, 
        file_path: str | None = None,
        force: bool = False
    ) -> str:
        try:
            manifest = fs.load_manifest(subject)
            categories = categories_filter.split(",") if categories_filter else None
            files = fs.discover_subject_files(
                subject, week=week, categories=categories, file_path=file_path
            )
    
            parsed, skipped, degraded, failed = [], [], [], []
            total_images = 0
    
            for source in files:
                key = str(source.path)
                file_hash = source.sha256()
                if not force and manifest.get(key, {}).get("hash") == file_hash:
                    skipped.append(key)
                    continue
    
                try:
                    text, was_degraded, images = parse_file_to_text(source.path)
                except Exception as exc:  # noqa: BLE001 - surfaced in report, not raised
                    failed.append(f"{key}: {exc}")
                    continue
    
                if not text.strip():
                    failed.append(f"{key}: parsed to empty text")
                    continue
    
                chunks = [
                    {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}::{i}")),
                        "text": chunk,
                        "metadata": {
                            "subject": source.subject,
                            "week": source.week or 0,
                            "category": source.category,
                            "source_file": key,
                        },
                    }
                    for i, chunk in enumerate(_chunk_text(text))
                ]
    
                target_collection = (
                    vector_store.pyq_collection_name(subject)
                    if source.category == "pyq"
                    else vector_store.parsed_collection_name(subject)
                )
                vector_store.add_chunks(target_collection, chunks)
    
                if images and source.category != "pyq":
                    total_images += save_and_index_images(
                        subject=subject, source_file=key, week=source.week, images=images, parsed_text=text
                    )
    
                manifest[key] = {"hash": file_hash, "chunks": len(chunks)}
                parsed.append(key)
                if was_degraded:
                    degraded.append(key)
    
            fs.save_manifest(subject, manifest)

            lines = [
                f"Parsed {len(parsed)} file(s), skipped {len(skipped)} unchanged, {len(failed)} failed."
            ]
            if parsed:
                lines.append("Parsed files: " + ", ".join(parsed[:25]))
            if skipped:
                lines.append("Skipped (unchanged) files: " + ", ".join(skipped[:25]))
            if total_images:
                lines.append(f"Extracted {total_images} diagram(s)/figure(s) for use in generated questions.")
            if degraded:
                lines.append(
                    "Degraded (images/diagrams not understood - install LibreOffice "
                    "and/or set GEMINI_API_KEY for full parsing): " + ", ".join(degraded)
                )
            if failed:
                lines.append("Failed: " + "; ".join(failed))

            # Always include a complete status summary across all 4 folder categories
            try:
                all_disk_files = fs.discover_subject_files(subject)
                gaps = fs.missing_weeks(subject)
                covered_weeks = [w for w in range(1, fs.NUM_WEEKS + 1) if w not in gaps]
                cat_map = {
                    "week": ("weeks (week_1..week_12)", [f for f in all_disk_files if f.category == "week"]),
                    "pyq": (f"pyq (docs/{subject}/pyq)", [f for f in all_disk_files if f.category == "pyq"]),
                    "transcript": (f"transcripts (docs/{subject}/transcripts)", [f for f in all_disk_files if f.category == "transcript"]),
                    "additional": (f"additional (docs/{subject}/additional)", [f for f in all_disk_files if f.category == "additional"]),
                }
                lines.append("Folder / Category Status:")
                for cat_key, (label, cat_files) in cat_map.items():
                    ingested_files = [f for f in cat_files if str(f.path) in manifest]
                    cat_chunks = sum(
                        int(manifest.get(str(f.path), {}).get("chunks", 0))
                        for f in ingested_files
                    )
                    extra = ""
                    if cat_key == "week":
                        w_str = ", ".join(str(w) for w in covered_weeks) if covered_weeks else "none"
                        extra = f" [covered weeks: {w_str}]"
                    lines.append(
                        f"- {label}: {len(cat_files)} file(s) on disk, "
                        f"{len(ingested_files)} ingested ({cat_chunks} chunks){extra}"
                    )
            except Exception:
                pass

            return "\n".join(lines)
        except Exception as e:
            return f"Error executing parser: {e}"
