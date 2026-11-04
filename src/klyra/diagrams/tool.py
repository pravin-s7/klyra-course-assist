"""CrewAI BaseTools (`GenerateDiagramTool`, `GenerateGraphTool`) for diagram generation."""
from __future__ import annotations

import hashlib
import uuid
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.diagrams.compiler import (
    LatexNotAvailableError,
    _normalize_tikz_body,
    render_latex_to_png,
)
from klyra.diagrams.verifier import (
    _detect_structural_tikz_issues,
    _regenerate_tikz_with_llm,
    _retrieve_course_and_pyq_notation,
    verify_diagram_png_visually,
)
from klyra.tools import vector_store
from klyra.utils import fs

MAX_DIAGRAM_RETRIES = 3


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
            "CRITICAL VISUAL QUALITY RULES:\n"
            "1. For GRAPHS: Use clean black-and-white textbook nodes (`circle, draw=black, fill=white, thick, minimum size=8.5mm`) "
            "placed in a non-collinear convex polygon (pentagon/hexagon/staggered layout, at least 2.8cm-3.5cm apart) so no edge ever cuts through another node. "
            "For CT, graphs MUST be DIRECTED (`->, >={Stealth[length=3.5mm,width=2.4mm]}`).\n"
            "2. For FLOWCHARTS / PROCEDURE FLOWS / ER DIAGRAMS / CIRCUITS: Use border-to-border relative positioning (`below=1.5cm of nodeA`, `right=2.4cm of nodeA`) "
            "so `diamond` decision nodes and process boxes never overlap and arrows always have long visible shafts. Put `fill=white, inner sep=2pt` on branch/edge labels ('Yes', 'No', weights)."
        ),
    )
    caption: str = Field(..., description="Short caption describing the diagram (used in the filename and alt text)")
    ground_truth: str = Field(
        ...,
        description=(
            "The FULL factual content of the diagram in plain text - e.g. every "
            "node and edge ('nodes A,B,C,D,E; edges A->B, A->C, B->D, C->D, D->E'), "
            "every flowchart step/branch, every circuit gate connection, or every cell of a table. "
            "This is indexed so the Discrepancy Review and Solve & Verify agents can verify "
            "the question matches the diagram."
        ),
    )
    week: int | None = Field(default=None, description="Week 1-12 this diagram is for, if known")


class GenerateDiagramTool(BaseTool):
    name: str = "generate_diagram"
    description: str = (
        "Render and visually verify a NEW diagram (graph, procedure flowchart, "
        "logic circuit, ER diagram, tree, B-tree, state machine, table) via "
        "LaTeX/TikZ. Pre-loaded libraries: trees, graphs, graphs.standard, "
        "arrows.meta, positioning, er, matrix, shapes, shapes.geometric, "
        "shapes.gates.logic.US, shapes.gates.logic.IEC, automata, calc, bending. "
        "Automatically normalizes node spacing and textbook styling, runs geometric "
        "and Gemini Vision verification on the rendered PNG (checking for edges "
        "cutting through nodes, overlapping flowchart diamonds/boxes, short/missing "
        "arrows, or lines cutting through labels), and automatically revises and "
        "regenerates the diagram if any defect is found. "
        "Returns ready-to-paste markdown image syntax on success."
    )
    args_schema: Type[BaseModel] = GenerateDiagramInput

    def _run(
        self, subject: str, latex_body: str, caption: str, ground_truth: str, week: int | None = None
    ) -> str:
        from klyra.services.skill_service import load_subject_skill
        from klyra.tools import latex_diagram_tool as _ldt

        skill = load_subject_skill(subject)
        diagram_rules = _ldt._retrieve_course_and_pyq_notation(
            subject=subject, caption=caption, ground_truth=ground_truth, week=week
        )

        current_body = _ldt._normalize_tikz_body(latex_body, subject=subject)
        png_bytes: bytes | None = None

        for attempt in range(_ldt.MAX_DIAGRAM_RETRIES + 1):
            try:
                png_bytes = _ldt.render_latex_to_png(current_body)
            except LatexNotAvailableError as exc:
                return f"{exc} Write this question as text-only instead."
            except Exception as exc:  # noqa: BLE001
                if attempt < _ldt.MAX_DIAGRAM_RETRIES:
                    repaired = _ldt._regenerate_tikz_with_llm(
                        latex_body=current_body,
                        issues=[f"LaTeX compilation error: {exc}"],
                        caption=caption,
                        ground_truth=ground_truth,
                        subject=subject,
                        diagram_rules=diagram_rules,
                    )
                    if repaired:
                        current_body = _ldt._normalize_tikz_body(repaired, subject=subject)
                        continue
                rules_hint = (
                    f"\nSubject diagram conventions ({skill.diagram_style}):\n{diagram_rules}"
                    if diagram_rules
                    else ""
                )
                return (
                    f"LaTeX failed to compile - fix it and retry, or write this question as text-only:\n"
                    f"{exc}{rules_hint}"
                )

            structural_issues = _ldt._detect_structural_tikz_issues(
                current_body, ground_truth=ground_truth, subject=subject
            )
            vis_passed, vis_issues, fixed_from_vision = _ldt.verify_diagram_png_visually(
                png_bytes=png_bytes,
                latex_body=current_body,
                caption=caption,
                ground_truth=ground_truth,
                subject=subject,
                diagram_rules=diagram_rules,
            )
            all_issues = structural_issues + vis_issues
            if not all_issues and vis_passed:
                break

            if attempt < _ldt.MAX_DIAGRAM_RETRIES:
                candidate_fix: str | None = None
                if fixed_from_vision and not structural_issues:
                    candidate_fix = fixed_from_vision
                else:
                    candidate_fix = _ldt._regenerate_tikz_with_llm(
                        latex_body=current_body,
                        issues=all_issues,
                        caption=caption,
                        ground_truth=ground_truth,
                        subject=subject,
                        diagram_rules=diagram_rules,
                        png_bytes=png_bytes,
                    ) or fixed_from_vision
                if candidate_fix and candidate_fix.strip() != current_body.strip():
                    current_body = _ldt._normalize_tikz_body(candidate_fix, subject=subject)
                    continue
            break

        assert png_bytes is not None
        out_dir = fs.assets_dir(subject) / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(current_body.encode()).hexdigest()[:10]
        image_path = out_dir / f"{fs.slugify(caption)}-{digest}.png"
        image_path.write_bytes(png_bytes)

        try:
            vector_store.add_chunks(
                vector_store.images_collection_name(subject),
                [
                    {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"generated::{digest}")),
                        "text": f"{caption}. {ground_truth}",
                        "image_bytes": png_bytes,
                        "image_mime_type": "image/png",
                        "metadata": {
                            "subject": subject,
                            "week": week or 0,
                            "source_file": "generated",
                            "page": 0,
                            "diagram_style": skill.diagram_style,
                            "image_path": fs.markdown_image_path(subject, image_path),
                        },
                    }
                ],
            )
        except Exception:
            pass

        return f"![{caption}]({fs.markdown_image_path(subject, image_path)})"


class GenerateGraphInput(BaseModel):
    subject: str
    latex_body: str = Field(
        ...,
        description=(
            "LaTeX for the graph - only the content that goes inside "
            "\\begin{document}...\\end{document} (e.g. a full "
            "\\begin{tikzpicture}...\\end{tikzpicture} block). "
            "CRITICAL: Use clean black-and-white textbook nodes (`circle, draw=black, fill=white, thick, minimum size=8.5mm`) "
            "arranged in a non-collinear convex polygon (pentagon/hexagon layout) so no edge ever cuts through another node. "
            "For Computational Thinking (CT), it MUST be a DIRECTED graph (`->, >={Stealth[length=3.5mm,width=2.4mm]}`)."
        ),
    )
    caption: str = Field(..., description="Short caption describing the graph")
    ground_truth: str = Field(
        ...,
        description=(
            "The FULL factual content of the graph in plain text - e.g. every "
            "node and edge ('nodes 0,1,2,3,4; edges 0->1, 1->2, 2->0, 2->3, 3->0, 0->4, 1->4, 4->2'). "
            "This is indexed so other agents can verify the content. Never leave this out."
        ),
    )
    week: int | None = Field(default=None, description="Week 1-12 this graph is for, if known")


class GenerateGraphTool(GenerateDiagramTool):
    name: str = "generate_graph"
    description: str = (
        "Render and visually verify a NEW graph (directed, undirected, weighted, trees) via LaTeX/TikZ. "
        "Enforces clean black-and-white textbook graph styling, non-collinear vertex placement "
        "(so edges never cut through nodes), and prominent Stealth arrowheads. "
        "Automatically inspects the rendered PNG with Gemini Vision and regenerates "
        "the graph if any edge/node collision, invisible arrow, or label overlap is detected."
    )
    args_schema: Type[BaseModel] = GenerateGraphInput

