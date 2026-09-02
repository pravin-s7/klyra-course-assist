"""Generates NEW diagrams (trees, graphs, B-trees, ER diagrams, tables, ...)
via LaTeX/TikZ, for when no existing extracted image matches (see
image_tools.py's search_images, which should always be tried first).

Chosen over an AI image-generation model deliberately: LaTeX/TikZ
compilation is exact and deterministic - if the agent reasons about a
structure correctly (e.g. traces a B-tree insertion algorithm correctly),
the render is guaranteed structurally correct too. A generative image model
can look plausible while being visually wrong (wrong node count, wrong
edges, wrong labels), which is a real correctness risk for a tool that
produces exam questions.

Requires a LaTeX distribution on PATH (pdflatex + tikz/pgfplots/booktabs/
standalone) - same "optional, degrade with a clear message" pattern as the
LibreOffice dependency for .ppt/.doc parsing. No API/quota cost either way.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.tools import vector_store
from klyra.utils import fs

DEFAULT_PREAMBLE = r"""\documentclass[border=4pt]{standalone}
\usepackage{tikz}
\usepackage{pgfplots}
\usepackage{booktabs}
\usetikzlibrary{trees,graphs,graphs.standard,arrows.meta,positioning,er,matrix,shapes}
"""


class LatexNotAvailableError(RuntimeError):
    """Raised when no LaTeX distribution (pdflatex) is on PATH."""


def _pdflatex_path() -> str | None:
    return shutil.which("pdflatex")


def render_latex_to_png(body: str, preamble: str = DEFAULT_PREAMBLE, dpi: int = 200) -> bytes:
    """`body` is the LaTeX content that goes between \\begin{document} and
    \\end{document} - e.g. a tikzpicture or tabular block. Returns PNG bytes."""
    pdflatex = _pdflatex_path()
    if not pdflatex:
        raise LatexNotAvailableError(
            "No LaTeX distribution found (pdflatex not on PATH). Install e.g. "
            "`texlive-latex-extra texlive-pictures` (Linux) or MacTeX (macOS) "
            "to enable generating new diagrams."
        )

    tex_source = f"{preamble}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"

    with tempfile.TemporaryDirectory() as tmp:
        tex_file = Path(tmp) / "diagram.tex"
        tex_file.write_text(tex_source)

        result = subprocess.run(
            [pdflatex, "-interaction=nonstopmode", "-halt-on-error", "diagram.tex"],
            cwd=tmp,
            capture_output=True,
            timeout=60,
            text=True,
        )
        pdf_file = Path(tmp) / "diagram.pdf"
        if not pdf_file.exists():
            # pdflatex's own log has the actual error; stdout tail is usually enough context
            raise RuntimeError(f"LaTeX compilation failed:\n{result.stdout[-2000:]}")

        import pymupdf

        with pymupdf.open(str(pdf_file)) as doc:
            pix = doc[0].get_pixmap(dpi=dpi)
            return pix.tobytes("png")


class GenerateDiagramInput(BaseModel):
    subject: str
    latex_body: str = Field(
        ...,
        description=(
            "LaTeX for the diagram - only the content that goes inside "
            "\\begin{document}...\\end{document} (e.g. a full "
            "\\begin{tikzpicture}...\\end{tikzpicture} block, or a "
            "\\begin{tabular}...\\end{tabular}). Do not include "
            "\\documentclass or \\begin{document}. "
            "CRITICAL: Adapt your TikZ/LaTeX structures to the precise semantics of the "
            "subject! For example, if the subject is Computational Thinking (CT) and you are "
            "drawing a graph, it MUST be a DIRECTED graph (use -> edges) unless specified otherwise. "
            "If it is DBMS, use precise standard ER diagram symbols (diamonds for relations, etc). "
            "If it is Digital Electronics, use standard logic gate shapes. "
            "Always honor the domain conventions."
        ),
    )
    caption: str = Field(..., description="Short caption describing the diagram (used in the filename and alt text)")
    ground_truth: str = Field(
        ...,
        description=(
            "The FULL factual content of the diagram in plain text - e.g. every "
            "node and edge ('nodes A,B,C,D,E; edges A->B, A->C, B->D, C->D, D->E'), "
            "or every cell of a table. This is indexed so the Discrepancy Review "
            "and Solve & Verify agents - which cannot visually see the rendered "
            "image, only text - can still check the question is answerable and "
            "correct. Never leave this out or make it vaguer than the diagram "
            "itself; it is not shown to students, only used for verification."
        ),
    )
    week: int | None = Field(default=None, description="Week 1-12 this diagram is for, if known")


class GenerateDiagramTool(BaseTool):
    name: str = "generate_diagram"
    description: str = (
        "Render a NEW diagram (tree, B-tree, graph, ER diagram, table, "
        "dependency diagram, etc.) via LaTeX/TikZ, for when search_images "
        "found no existing match (always try search_images first - reuse a "
        "real diagram over generating one when a real one fits). Write the "
        "LaTeX yourself: tikz libraries trees, graphs, graphs.standard, "
        "arrows.meta, positioning, er, matrix, shapes are pre-loaded, plus "
        "pgfplots and booktabs. "
        "CRITICAL DOMAIN ACCURACY: You must ensure standard conventions are met "
        "(e.g. Computational Thinking / CT graphs must be directed by default, "
        "DBMS diagrams must use proper ER notation, circuits must use standard gates). "
        "You MUST also supply ground_truth (the "
        "diagram's full factual content in text) so other agents that can't "
        "see the image can still verify/solve the question - this is indexed "
        "into search_images, not shown to students. Returns ready-to-paste "
        "markdown image syntax on success, or a clear compile error you "
        "should fix and retry once - if it still fails, or no LaTeX "
        "distribution is installed, write the question as text-only instead."
    )
    args_schema: Type[BaseModel] = GenerateDiagramInput

    def _run(
        self, subject: str, latex_body: str, caption: str, ground_truth: str, week: int | None = None
    ) -> str:
        try:
            png_bytes = render_latex_to_png(latex_body)
        except LatexNotAvailableError as exc:
            return f"{exc} Write this question as text-only instead."
        except Exception as exc:  # noqa: BLE001 - compile errors surfaced to the agent to fix/retry
            return f"LaTeX failed to compile - fix it and retry, or write this question as text-only:\n{exc}"

        out_dir = fs.assets_dir(subject) / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(latex_body.encode()).hexdigest()[:10]
        image_path = out_dir / f"{fs.slugify(caption)}-{digest}.png"
        image_path.write_bytes(png_bytes)

        vector_store.add_chunks(
            vector_store.images_collection_name(subject),
            [
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"generated::{digest}")),
                    "text": f"{caption}. {ground_truth}",
                    "metadata": {
                        "subject": subject,
                        "week": week or 0,
                        "source_file": "generated",
                        "page": 0,
                        "image_path": fs.markdown_image_path(subject, image_path),
                    },
                }
            ],
        )

        return f"![{caption}]({fs.markdown_image_path(subject, image_path)})"

class GenerateGraphInput(BaseModel):
    subject: str
    latex_body: str = Field(
        ...,
        description=(
            "LaTeX for the graph - only the content that goes inside "
            "\\begin{document}...\\end{document} (e.g. a full "
            "\\begin{tikzpicture}...\\end{tikzpicture} block). "
            "CRITICAL: Adapt your TikZ/LaTeX structures to the precise semantics of the "
            "subject! For example, if the subject is Computational Thinking (CT), it MUST be a "
            "DIRECTED graph (use -> edges) unless specified otherwise. Always honor domain conventions."
        ),
    )
    caption: str = Field(..., description="Short caption describing the graph")
    ground_truth: str = Field(
        ...,
        description=(
            "The FULL factual content of the graph in plain text - e.g. every "
            "node and edge ('nodes A,B,C,D; edges A->B, A->C, B->D'). "
            "This is indexed so other agents can verify the content. Never leave this out."
        ),
    )
    week: int | None = Field(default=None, description="Week 1-12 this graph is for, if known")


class GenerateGraphTool(GenerateDiagramTool):
    name: str = "generate_graph"
    description: str = (
        "Render a NEW graph (directed, undirected, weighted, trees, etc.) via LaTeX/TikZ. "
        "Use this tool specifically when you need to generate a graph. "
        "Write the LaTeX yourself: tikz libraries trees, graphs, graphs.standard, "
        "arrows.meta, positioning are pre-loaded. "
        "CRITICAL DOMAIN ACCURACY: You must ensure standard conventions are met "
        "(e.g. Computational Thinking graphs must be directed by default). "
        "You MUST also supply ground_truth (the graph's full factual content in text). "
        "Returns ready-to-paste markdown image syntax on success, or a compile error."
    )
    args_schema: Type[BaseModel] = GenerateGraphInput
