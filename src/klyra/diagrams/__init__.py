"""LaTeX/TikZ diagram compilation, geometric & vision verification, and CrewAI tools."""
from __future__ import annotations

from klyra.diagrams.compiler import (
    DEFAULT_PREAMBLE,
    LatexNotAvailableError,
    _are_three_collinear,
    _convex_polygon_coords,
    _decollinearize_graph_nodes,
    _is_flowchart_or_block_diagram,
    _normalize_tikz_body,
    _pdflatex_path,
    _strip_code_fences,
    render_latex_to_png,
)
from klyra.diagrams.tool import (
    MAX_DIAGRAM_RETRIES,
    GenerateDiagramInput,
    GenerateDiagramTool,
    GenerateGraphInput,
    GenerateGraphTool,
)
from klyra.diagrams.verifier import (
    _detect_structural_tikz_issues,
    _estimate_node_half_extents_cm,
    _extract_json_dict,
    _point_to_segment_dist,
    _regenerate_tikz_with_llm,
    _retrieve_course_and_pyq_notation,
    _vision_candidate_models,
    verify_diagram_png_visually,
)

__all__ = [
    "DEFAULT_PREAMBLE",
    "LatexNotAvailableError",
    "MAX_DIAGRAM_RETRIES",
    "GenerateDiagramInput",
    "GenerateDiagramTool",
    "GenerateGraphInput",
    "GenerateGraphTool",
    "render_latex_to_png",
    "verify_diagram_png_visually",
    "_normalize_tikz_body",
    "_detect_structural_tikz_issues",
    "_regenerate_tikz_with_llm",
    "_retrieve_course_and_pyq_notation",
    "_strip_code_fences",
    "_pdflatex_path",
    "_is_flowchart_or_block_diagram",
    "_decollinearize_graph_nodes",
    "_are_three_collinear",
    "_convex_polygon_coords",
    "_estimate_node_half_extents_cm",
    "_point_to_segment_dist",
    "_extract_json_dict",
    "_vision_candidate_models",
]

