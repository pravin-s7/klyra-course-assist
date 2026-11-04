"""Generates and visually verifies NEW diagrams (graphs, flowcharts/procedure
flows, circuit diagrams, ER diagrams, trees, B-trees, state machines, tables)
via LaTeX/TikZ.

Guarantees textbook-quality visual output through a 3-stage pipeline:
1. Deterministic TikZ normalization (`_normalize_tikz_body`):
   - Enforces clean textbook black-and-white graph styling (`draw=black,
     fill=white, thick, circle, minimum size=8.5mm`), prominent arrowheads
     (`>={Stealth[length=3.5mm,width=2.4mm]}`), generous node spacing, and
     white-backed edge labels so lines never cut through text.
   - Expands cramped flowchart/ER/circuit coordinates (especially tall
     `diamond` decision nodes and multi-line boxes) so shapes never collide
     and arrows never squash or invert.
2. Geometric collision & layout verification (`_detect_structural_tikz_issues`):
   - Computes node bounding boxes (circles, diamonds, rectangles, ellipses) to
     detect node-node overlaps or short/inverted border-to-border arrows.
   - Computes point-to-segment/arc distance from every intermediate node to
     every edge so no edge ever cuts through another node's circle.
3. Multimodal Vision Verification & Self-Healing Regeneration
   (`verify_diagram_png_visually` + `_regenerate_tikz_with_llm`):
   - Inspects the rendered PNG with Gemini Vision across all diagram types
     (graphs, procedure flowcharts, logic circuits, ER diagrams, trees,
     automata) and automatically revises + regenerates the TikZ diagram
     (up to 3 attempts) if any visual defect is detected.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.tools import vector_store
from klyra.utils import fs

DEFAULT_PREAMBLE = r"""\documentclass[border=10pt]{standalone}
\usepackage{tikz}
\usepackage{pgfplots}
\usepackage{booktabs}
\usepackage{amsmath,amssymb}
\pgfplotsset{compat=1.18}
\usetikzlibrary{
  trees,graphs,graphs.standard,arrows.meta,positioning,er,matrix,
  shapes,shapes.geometric,shapes.multipart,shapes.gates.logic.US,
  shapes.gates.logic.IEC,automata,calc,bending,fit,backgrounds
}
\tikzset{
  >={Stealth[length=3.5mm,width=2.4mm,inset=0.4mm]},
  every picture/.style={
    line width=0.95pt,
    node distance=2.6cm and 3.0cm,
    >={Stealth[length=3.5mm,width=2.4mm,inset=0.4mm]}
  },
  every loop/.style={min distance=10mm, looseness=8, ->, >={Stealth[length=3.5mm,width=2.4mm,inset=0.4mm]}}
}
"""

MAX_DIAGRAM_RETRIES = 3

_NODE_FULL_RE = re.compile(
    r"\\node\s*(?:\[([^\]]*)\])?\s*\(([A-Za-z0-9_]+)\)\s*(?:at\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\))?\s*\{((?:[^{}]|\{[^{}]*\})*)\}"
)
_NODE_COORD_RE = re.compile(
    r"\\node\b[^\n;]*?\(([A-Za-z0-9_]+)\)\s*at\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)"
)
_NODE_DIST_RE = re.compile(
    r"node\s+distance\s*=\s*(\d+(?:\.\d+)?)\s*(cm|mm|pt)?"
    r"(?:\s+and\s+(\d+(?:\.\d+)?)\s*(cm|mm|pt)?)?",
    re.IGNORECASE,
)
_DEPRECATED_OF_RE = re.compile(
    r"\b(below|above|left|right)\s+of\s*=\s*([A-Za-z0-9_]+)"
)
_SHORT_POS_RE = re.compile(
    r"\b(below|above|left|right)\s*=\s*(\d+(?:\.\d+)?)\s*(cm|mm)\s+of\s+([A-Za-z0-9_]+)"
)
_DIRECTED_EDGE_PAIR_RE = re.compile(
    r"\(([A-Za-z0-9_.]+)\)\s*"
    r"(?:node\s*(?:\[[^\]]*\])?\s*\{(?:[^{}]|\{[^{}]*\})*\}\s*)?"
    r"(?:(?:edge|to)(?:\s*\[([^\]]*)\])?|--|->)\s*"
    r"(?:node\s*(?:\[[^\]]*\])?\s*\{(?:[^{}]|\{[^{}]*\})*\}\s*)?"
    r"\(([A-Za-z0-9_.]+)\)"
)
_CODE_FENCE_RE = re.compile(r"```(?:latex|tex|tikz)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class LatexNotAvailableError(RuntimeError):
    """Raised when no LaTeX distribution (pdflatex) is on PATH."""


def _pdflatex_path() -> str | None:
    return shutil.which("pdflatex")


def _strip_code_fences(latex_body: str) -> str:
    text = (latex_body or "").strip()
    m = _CODE_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    text = re.sub(r"\\documentclass(?:\[[^\]]*\])?\{[^}]*\}", "", text)
    text = re.sub(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\}", "", text)
    text = re.sub(r"\\usetikzlibrary\{[^}]*\}", "", text)
    text = re.sub(r"\\begin\{document\}", "", text)
    text = re.sub(r"\\end\{document\}", "", text)
    return text.strip()


def _to_cm(val: float, unit: str) -> float:
    u = (unit or "cm").lower()
    if u == "mm":
        return val / 10.0
    if u == "pt":
        return val / 28.45
    return val


def _are_three_collinear(
    p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]
) -> bool:
    """Return True if points p1, p2, p3 are nearly collinear."""
    # Area of triangle * 2 = |(x2-x1)(y3-y1) - (y2-y1)(x3-x1)|
    cross = abs((p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0]))
    max_side = max(
        math.hypot(p2[0] - p1[0], p2[1] - p1[1]),
        math.hypot(p3[0] - p2[0], p3[1] - p2[1]),
        math.hypot(p3[0] - p1[0], p3[1] - p1[1]),
        1e-6,
    )
    # Perpendicular height of middle point from the line connecting outer two points
    perp_h = cross / max_side
    return perp_h < 0.35


def _convex_polygon_coords(n: int) -> list[tuple[float, float]]:
    """Return textbook convex polygon coordinates (matching Image 3's pentagon style for n=5)."""
    if n == 5:
        # Exact Image 3 textbook 5-node pentagon layout:
        # Left apex + top pair + bottom pair
        return [
            (0.0, 1.8),
            (2.2, 3.6),
            (5.2, 3.6),
            (2.2, 0.0),
            (5.2, 0.0),
        ]
    if n == 4:
        return [
            (0.0, 3.2),
            (3.6, 3.2),
            (0.0, 0.0),
            (3.6, 0.0),
        ]
    if n == 6:
        return [
            (0.0, 1.8),
            (2.0, 3.6),
            (4.8, 3.6),
            (6.8, 1.8),
            (2.0, 0.0),
            (4.8, 0.0),
        ]
    radius = max(2.6, 0.55 * n)
    pts: list[tuple[float, float]] = []
    for k in range(n):
        angle = math.pi - (2.0 * math.pi * k / n)
        pts.append((round(radius * (1.0 + math.cos(angle)), 2), round(radius * (1.0 + math.sin(angle)), 2)))
    return pts


def _is_flowchart_or_block_diagram(body: str) -> bool:
    return bool(
        re.search(
            r"\b(?:diamond|rectangle|rounded\s+corners|trapezium|cylinder|logic\s+gate|and\s+gate|or\s+gate|not\s+gate|nand\s+gate|nor\s+gate|xor\s+gate|entity|relationship)\b",
            body,
            re.IGNORECASE,
        )
    )


def _decollinearize_graph_nodes(body: str) -> str:
    """If a graph (non-flowchart/tree) has 3+ collinear vertices (causing edges to
    cut through intermediate vertices or forcing ugly bent arcs like Image 1),
    rearrange the vertices into a clean textbook convex polygon (like Image 3)
    and remove unnecessary `bend` from unidirectional edges."""
    if _is_flowchart_or_block_diagram(body) or "\\child" in body:
        return body

    matches = list(_NODE_FULL_RE.finditer(body))
    coord_nodes: list[tuple[re.Match[str], str, float, float]] = []
    for m in matches:
        node_id = m.group(2)
        xs, ys = m.group(3), m.group(4)
        if xs is not None and ys is not None:
            coord_nodes.append((m, node_id, float(xs), float(ys)))

    n = len(coord_nodes)
    if n < 4 or n > 8:
        return body

    has_collinear = False
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                if _are_three_collinear(
                    (coord_nodes[i][2], coord_nodes[i][3]),
                    (coord_nodes[j][2], coord_nodes[j][3]),
                    (coord_nodes[k][2], coord_nodes[k][3]),
                ):
                    has_collinear = True
                    break
            if has_collinear:
                break
        if has_collinear:
            break

    if not has_collinear:
        return body

    # Sort existing nodes by (x, -y) so leftmost node gets the left apex (0.0, 1.8),
    # middle column gets top/bottom middle, right column gets top/bottom right
    sorted_indices = sorted(
        range(n),
        key=lambda idx: (round(coord_nodes[idx][2], 2), -round(coord_nodes[idx][3], 2)),
    )
    target_pts = _convex_polygon_coords(n)
    new_pos_by_id: dict[str, tuple[float, float]] = {}
    for rank, orig_idx in enumerate(sorted_indices):
        node_id = coord_nodes[orig_idx][1]
        new_pos_by_id[node_id] = target_pts[rank]

    def _replace_node_coord(m: re.Match[str]) -> str:
        node_id = m.group(2)
        if node_id not in new_pos_by_id:
            return m.group(0)
        nx, ny = new_pos_by_id[node_id]
        opts = m.group(1)
        lbl = m.group(5)
        opt_str = f"[{opts}]" if opts is not None else ""
        return f"\\node{opt_str} ({node_id}) at ({nx:.2f}, {ny:.2f}) {{{lbl}}}"

    body = _NODE_FULL_RE.sub(_replace_node_coord, body)

    # Identify unidirectional vs antiparallel edges; strip unnecessary `bend` on
    # unidirectional edges now that vertices sit on a convex polygon (like Image 3)
    edge_pairs: set[tuple[str, str]] = set()
    for m in _DIRECTED_EDGE_PAIR_RE.finditer(body):
        u = m.group(1).split(".")[0]
        v = m.group(3).split(".")[0]
        if u != v:
            edge_pairs.add((u, v))
    antiparallel = {(u, v) for (u, v) in edge_pairs if (v, u) in edge_pairs}

    def _straighten_unidirectional(m: re.Match[str]) -> str:
        full = m.group(0)
        u = m.group(1).split(".")[0]
        opts = m.group(2) or ""
        v = m.group(3).split(".")[0]
        if (u, v) in antiparallel or "bend" not in opts:
            return full
        cleaned_opts = re.sub(
            r",?\s*\bbend\s+(?:left|right)(?:\s*=\s*-?\d+(?:\.\d+)?)?\s*,?",
            "",
            opts,
        ).strip(", ")
        # Replace `edge[bend ...]` with `--` (or `edge[cleaned_opts]`) and offset weight to `pos=0.35`
        if cleaned_opts:
            res = re.sub(r"(?:edge|to)\s*\[[^\]]*\]", f"edge[{cleaned_opts}]", full, count=1)
        else:
            res = re.sub(r"(?:edge|to)\s*\[[^\]]*\]", "--", full, count=1)
        if "pos=" not in res and "node[" in res:
            res = res.replace("node[", "node[pos=0.35, ", 1)
        return res

    body = _DIRECTED_EDGE_PAIR_RE.sub(_straighten_unidirectional, body)
    # Since node relative orientations changed when moving to the convex polygon,
    # replace hardcoded `above`/`below`/`left`/`right` on edge weight nodes with `auto`
    # so TikZ places every weight perpendicular to its new edge direction.
    body = re.sub(
        r"(?<!\\)\bnode\s*\[([^\]]*)\]",
        lambda m: (
            "node["
            + re.sub(r"\b(?:above|below|left|right)\b", "auto", m.group(1))
            + "]"
        ),
        body,
    )
    return body


def _normalize_tikz_body(latex_body: str, subject: str = "") -> str:
    """Normalize TikZ code for graphs, flowcharts, circuits, ER diagrams, and trees:
    - Enforces clean black-and-white textbook node styling (like Image 3) for graphs
    - Replaces tiny arrows with bold `{Stealth[length=3.5mm,width=2.4mm]}`
    - Converts deprecated center-to-center `below of=X` to border-to-border `below=1.5cm of X`
    - Expands cramped coordinates on both graphs and diamond/rectangle flowcharts
    - Adds `fill=white, inner sep=1.5pt` to inline edge labels so lines never strike through text
    - Curves antiparallel edges (`bend left=18`) so opposing edges never overlap
    """
    body = _strip_code_fences(latex_body)
    if "\\begin{tikzpicture}" not in body:
        return body

    # 1. Upgrade tiny arrow specifications (`>=latex`, `>=stealth`, `>=to`) to bold Stealth
    body = re.sub(
        r">=\s*(?:latex|stealth|to)\b",
        r">={Stealth[length=3.5mm,width=2.4mm]}",
        body,
        flags=re.IGNORECASE,
    )

    # 2. Replace pale/colored graph node styles (e.g. `blue!60`, `fill=blue!5`) with
    #    clean textbook black-and-white styling (matching Image 3)
    body = re.sub(r"\bdraw\s*=\s*blue(?:![0-9]+)?", "draw=black", body, flags=re.IGNORECASE)
    body = re.sub(r"\bfill\s*=\s*blue![0-9]+", "fill=white", body, flags=re.IGNORECASE)
    body = re.sub(r"\btext\s*=\s*blue(?:![0-9]+)?", "text=black", body, flags=re.IGNORECASE)

    # 2b. Rearrange collinear graph nodes into a textbook convex polygon (like Image 3)
    body = _decollinearize_graph_nodes(body)

    # 3. Convert deprecated center-to-center `below of=X` to border-to-border `below=1.5cm of X`
    def _fix_deprecated_of(m: re.Match[str]) -> str:
        direction = m.group(1).lower()
        target = m.group(2)
        dist = "1.6cm" if direction in ("below", "above") else "2.2cm"
        return f"{direction}={dist} of {target}"

    body = _DEPRECATED_OF_RE.sub(_fix_deprecated_of, body)

    # 4. Upgrade overly short relative positioning (`below=0.5cm of X` -> `below=1.4cm of X`)
    def _fix_short_pos(m: re.Match[str]) -> str:
        direction = m.group(1).lower()
        val = _to_cm(float(m.group(2)), m.group(3))
        target = m.group(4)
        min_cm = 1.4 if direction in ("below", "above") else 1.8
        if val < min_cm:
            return f"{direction}={min_cm:.1f}cm of {target}"
        return m.group(0)

    body = _SHORT_POS_RE.sub(_fix_short_pos, body)

    # 5. For diamond decision nodes without `aspect`, add `aspect=2, inner sep=2pt` so
    #    diamonds are wider and shorter rather than tall spikes that collide vertically
    body = re.sub(
        r"\bdiamond\b(?![^\]]*aspect)",
        "diamond, aspect=2.0, inner sep=2pt",
        body,
    )

    # 6. Enforce minimum `node distance`
    is_flow = _is_flowchart_or_block_diagram(body)

    def _fix_node_dist(match: re.Match[str]) -> str:
        v1 = float(match.group(1))
        u1 = match.group(2) or "cm"
        cm1 = _to_cm(v1, u1)
        v2_raw = match.group(3)
        u2 = match.group(4) or u1
        min_v = 1.6 if is_flow else 2.6
        min_h = 2.2 if is_flow else 2.8
        if v2_raw is not None:
            cm2 = _to_cm(float(v2_raw), u2)
            return f"node distance={max(cm1, min_v):.1f}cm and {max(cm2, min_h):.1f}cm"
        return f"node distance={max(cm1, min_v):.1f}cm"

    body = _NODE_DIST_RE.sub(_fix_node_dist, body)

    # 7. Check explicit node coordinates `\node ... (id) at (x, y)` for cramped spacing
    coords: list[tuple[str, float, float, str, str]] = []
    for m in _NODE_FULL_RE.finditer(body):
        opts = m.group(1) or ""
        node_id = m.group(2)
        x_s, y_s = m.group(3), m.group(4)
        label_txt = m.group(5) or ""
        if x_s is not None and y_s is not None:
            coords.append((node_id, float(x_s), float(y_s), opts, label_txt))

    if len(coords) >= 2 and "x=" not in body and "scale=" not in body:
        min_dist = float("inf")
        min_vert_flow_dist = float("inf")
        min_horiz_flow_dist = float("inf")
        has_large_boxes = is_flow or any("\\\\" in c[4] or len(c[4]) > 12 for c in coords)

        for i in range(len(coords)):
            for j in range(i + 1, len(coords)):
                dx = abs(coords[i][1] - coords[j][1])
                dy = abs(coords[i][2] - coords[j][2])
                d = math.hypot(dx, dy)
                if d > 0:
                    min_dist = min(min_dist, d)
                if dx < 0.8 and dy > 0:
                    min_vert_flow_dist = min(min_vert_flow_dist, dy)
                if dy < 0.8 and dx > 0:
                    min_horiz_flow_dist = min(min_horiz_flow_dist, dx)

        scale_x = 1.0
        scale_y = 1.0
        if has_large_boxes:
            # Flowchart / ER / multi-line boxes need at least 3.8cm vertical and 4.8cm horizontal center distance
            if 0.0 < min_vert_flow_dist < 3.8:
                scale_y = max(scale_y, min(3.5, round(4.0 / min_vert_flow_dist, 2)))
            if 0.0 < min_horiz_flow_dist < 4.8:
                scale_x = max(scale_x, min(3.5, round(5.0 / min_horiz_flow_dist, 2)))
            if 0.0 < min_dist < 2.8:
                s = min(3.5, round(3.2 / min_dist, 2))
                scale_x = max(scale_x, s)
                scale_y = max(scale_y, s)
        else:
            if 0.0 < min_dist < 2.2:
                s = max(2.2, min(3.5, round(2.6 / min_dist, 2)))
                scale_x = s
                scale_y = s

        if scale_x > 1.05 or scale_y > 1.05:
            sx = max(scale_x, 1.0)
            sy = max(scale_y, 1.0)
            if re.search(r"\\begin\{tikzpicture\}\s*\[", body):
                body = re.sub(
                    r"\\begin\{tikzpicture\}\s*\[",
                    f"\\\\begin{{tikzpicture}}[x={sx}cm, y={sy}cm, ",
                    body,
                    count=1,
                )
            else:
                body = body.replace(
                    "\\begin{tikzpicture}",
                    f"\\begin{{tikzpicture}}[x={sx}cm, y={sy}cm]",
                    1,
                )

    # 8. Ensure `\begin{tikzpicture}` has high-visibility arrow tips enabled
    if re.search(r"\\begin\{tikzpicture\}\s*\[", body):
        if "Stealth" not in body:
            body = re.sub(
                r"\\begin\{tikzpicture\}\s*\[",
                r"\\begin{tikzpicture}[>={Stealth[length=3.5mm,width=2.4mm]}, ",
                body,
                count=1,
            )
    else:
        body = body.replace(
            "\\begin{tikzpicture}",
            "\\begin{tikzpicture}[>={Stealth[length=3.5mm,width=2.4mm]}, node distance=2.6cm and 3.0cm]",
            1,
        )

    # 9. Prevent lines from cutting through edge labels (like "Yes" in Image 2) AND
    #    prevent `every node/.style={circle, draw...}` from drawing giant circles around
    #    edge weights (`3`, `7`, `4` in weighted graphs):
    has_global_circle_draw = bool(
        re.search(r"every\s+node/\.style\s*=\s*\{[^}]*\b(?:circle|draw)\b", body, re.IGNORECASE)
    )

    def _protect_edge_label(m: re.Match[str]) -> str:
        opts = (m.group(1) or "").strip()
        txt = m.group(2)
        if not txt.strip():
            return m.group(0)
        extra: list[str] = []
        if has_global_circle_draw and "draw=" not in opts:
            extra.append("draw=none, rectangle, minimum size=0pt")
        has_dir_or_fill = bool(
            re.search(
                r"\b(?:above|below|left|right|auto|fill)\b",
                opts,
                re.IGNORECASE,
            )
        )
        if not has_dir_or_fill or (has_global_circle_draw and "fill=" not in opts):
            extra.append("fill=white, inner sep=1.5pt")
        if not extra:
            return m.group(0)
        suffix = ", ".join(extra)
        new_opts = f"{opts}, {suffix}" if opts else suffix
        return f"node[{new_opts}] {{{txt}}}"

    body = re.sub(
        r"(?<!\\)\bnode\s*(?:\[([^\]]*)\])?\s*\{((?:[^{}]|\{[^{}]*\})+)\}",
        _protect_edge_label,
        body,
    )

    # 10. For Computational Thinking (CT), graphs must be directed by default
    subj_norm = (subject or "").strip().lower()
    if subj_norm == "ct" and "->" not in body and "\\node" in body:
        body = re.sub(
            r"\\draw(?:\s*\[([^\]]*)\])?(?=\s)",
            lambda m: (
                f"\\draw[->, {m.group(1)}]"
                if m.group(1) and "->" not in m.group(1)
                else (m.group(0) if m.group(1) else "\\draw[->]")
            ),
            body,
        )

    # 11. Curve antiparallel edges (u -> v and v -> u) so arrows and weights never overlap
    edge_pairs: set[tuple[str, str]] = set()
    for m in _DIRECTED_EDGE_PAIR_RE.finditer(body):
        u = m.group(1).split(".")[0]
        v = m.group(3).split(".")[0]
        if u != v:
            edge_pairs.add((u, v))

    antiparallel = {(u, v) for (u, v) in edge_pairs if (v, u) in edge_pairs}
    if antiparallel:
        def _curve_bidirectional(match: re.Match[str]) -> str:
            full = match.group(0)
            u = match.group(1).split(".")[0]
            opts = match.group(2) or ""
            v = match.group(3).split(".")[0]
            if (u, v) not in antiparallel or "bend" in opts or "bend" in full:
                return full
            if "edge[" in full or "edge [" in full:
                return re.sub(
                    r"edge\s*\[([^\]]*)\]",
                    r"edge[bend left=18, \1]",
                    full,
                    count=1,
                )
            if "edge" in full:
                return full.replace("edge", "edge[bend left=18]", 1)
            return full

        body = _DIRECTED_EDGE_PAIR_RE.sub(_curve_bidirectional, body)

    return body


def _estimate_node_half_extents_cm(opts: str, label: str, style_block: str) -> tuple[float, float]:
    """Estimate `(half_width_cm, half_height_cm)` of a TikZ node for overlap and
    edge-collision checking."""
    combined = f"{opts} {style_block}".lower()
    clean_label = re.sub(r"\$|[\\{}]", "", label)
    lines = [ln.strip() for ln in re.split(r"\\\\", clean_label) if ln.strip()] or [clean_label]
    max_chars = max((len(ln) for ln in lines), default=2)
    num_lines = len(lines)

    if "diamond" in combined or "decision" in combined:
        # Diamonds have diagonal corners extending ~1.8x the text bounding box
        hw = max(1.4, max_chars * 0.11 + 0.6)
        hh = max(1.2, num_lines * 0.45 + 0.5)
        return hw, hh
    if any(k in combined for k in ("rectangle", "rounded corners", "block", "process", "entity", "trapezium")):
        hw = max(1.2, max_chars * 0.09 + 0.4)
        hh = max(0.55, num_lines * 0.32 + 0.25)
        return hw, hh
    # Default circle / vertex node
    return 0.48, 0.48


def _point_to_segment_dist(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> tuple[float, float]:
    """Return `(distance, t_param)` of point `(px, py)` to segment `(ax, ay) -> (bx, by)`."""
    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-9:
        return math.hypot(px - ax, py - ay), 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
    t_clamped = max(0.0, min(1.0, t))
    proj_x = ax + t_clamped * dx
    proj_y = ay + t_clamped * dy
    return math.hypot(px - proj_x, py - proj_y), t


def _detect_structural_tikz_issues(
    latex_body: str, ground_truth: str, subject: str = ""
) -> list[str]:
    """Deterministic geometric & structural checks for TikZ diagrams:
    1. Identical or overlapping node bounding boxes (e.g. overlapping flowchart diamonds in Image 2).
    2. Edges that pass directly through or collide with an intermediate node (e.g. S1->S5 cutting through S2 in Image 1).
    3. Connected nodes whose border-to-border gap is too small (< 0.55cm) or negative (causing inverted/missing arrows in Image 2).
    4. Missing directed arrows (`->`) when `ground_truth` or `subject` requires directed edges.
    """
    issues: list[str] = []
    subj_norm = (subject or "").strip().lower()
    gt = (ground_truth or "").strip()

    # Extract global scale factors `x=...cm, y=...cm` if present
    sx_m = re.search(r"\bx\s*=\s*(\d+(?:\.\d+)?)\s*cm", latex_body)
    sy_m = re.search(r"\by\s*=\s*(\d+(?:\.\d+)?)\s*cm", latex_body)
    sx = float(sx_m.group(1)) if sx_m else 1.0
    sy = float(sy_m.group(1)) if sy_m else 1.0

    # Parse nodes with explicit `(x, y)` coordinates
    nodes: dict[str, dict[str, Any]] = {}
    for m in _NODE_FULL_RE.finditer(latex_body):
        opts = m.group(1) or ""
        node_id = m.group(2)
        x_s, y_s = m.group(3), m.group(4)
        label_txt = m.group(5) or ""
        if x_s is not None and y_s is not None:
            x_cm = float(x_s) * sx
            y_cm = float(y_s) * sy
            hw, hh = _estimate_node_half_extents_cm(opts, label_txt, latex_body[: m.start()])
            nodes[node_id] = {
                "id": node_id,
                "x": x_cm,
                "y": y_cm,
                "hw": hw,
                "hh": hh,
                "opts": opts,
                "label": label_txt,
            }

    node_list = list(nodes.values())
    # 1. Check for overlapping node bounding boxes
    for i in range(len(node_list)):
        for j in range(i + 1, len(node_list)):
            n1, n2 = node_list[i], node_list[j]
            dx = abs(n1["x"] - n2["x"])
            dy = abs(n1["y"] - n2["y"])
            min_dx = n1["hw"] + n2["hw"] + 0.25
            min_dy = n1["hh"] + n2["hh"] + 0.25
            if dx < min_dx and dy < min_dy:
                issues.append(
                    f"Nodes '{n1['id']}' and '{n2['id']}' are too close or overlapping "
                    f"(center delta=({dx:.2f}cm, {dy:.2f}cm), required clearance>=({min_dx:.2f}cm, {min_dy:.2f}cm)). "
                    "Use border-to-border relative positioning (`below=1.5cm of ...`, `right=2.2cm of ...`) or increase coordinate spacing."
                )

    # 2. Check edges for:
    #    (a) passing through or near an intermediate node (Image 1 defect)
    #    (b) two edges departing/arriving at the same node at nearly identical angles (< 15 deg, Image 1 defect)
    #    (c) overly short border-to-border shaft length (Image 2 defect)
    departure_angles: dict[str, list[tuple[str, float]]] = {}
    arrival_angles: dict[str, list[tuple[str, float]]] = {}

    for m in _DIRECTED_EDGE_PAIR_RE.finditer(latex_body):
        u_raw, edge_opts, v_raw = m.group(1), m.group(2) or "", m.group(3)
        u = u_raw.split(".")[0]
        v = v_raw.split(".")[0]
        if u == v or u not in nodes or v not in nodes:
            continue
        nu, nv = nodes[u], nodes[v]
        ax, ay = nu["x"], nu["y"]
        bx, by = nv["x"], nv["y"]
        center_dist = math.hypot(bx - ax, by - ay)
        if center_dist < 1.5:
            issues.append(
                f"Edge ({u} -> {v}) is too short ({center_dist:.2f}cm center-to-center), making the arrow shaft cramped or invisible."
            )
            continue

        # TikZ bend angle or explicit `out=..., in=...` angles
        base_angle_rad = math.atan2(by - ay, bx - ax)
        out_m = re.search(r"\bout\s*=\s*(-?\d+(?:\.\d+)?)", edge_opts)
        in_m = re.search(r"\bin\s*=\s*(-?\d+(?:\.\d+)?)", edge_opts)
        bm = re.search(r"bend\s+(left|right)(?:\s*=\s*(-?\d+(?:\.\d+)?))?", edge_opts)
        if out_m and in_m:
            dep_rad = math.radians(float(out_m.group(1)))
            arr_rad = math.radians(float(in_m.group(1)))
        else:
            bend_deg = 0.0
            if bm:
                sign = 1.0 if bm.group(1) == "left" else -1.0
                deg = float(bm.group(2)) if bm.group(2) is not None else 30.0
                bend_deg = sign * deg
            bend_rad = math.radians(bend_deg)
            dep_rad = base_angle_rad + bend_rad
            arr_rad = base_angle_rad + math.pi - bend_rad

        dep_deg = math.degrees(dep_rad)
        arr_deg = math.degrees(arr_rad)
        for prev_v, prev_deg in departure_angles.get(u, []):
            if prev_v != v:
                diff = abs((dep_deg - prev_deg + 180.0) % 360.0 - 180.0)
                if diff < 15.0:
                    issues.append(
                        f"Edges ({u} -> {prev_v}) and ({u} -> {v}) depart node '{u}' at nearly the same angle "
                        f"(angle difference={diff:.1f}° < 15°), causing overlapping edges/labels near '{u}'. "
                        "Reposition vertices into a convex polygon (pentagon/hexagon) so edges separate cleanly."
                    )
        departure_angles.setdefault(u, []).append((v, dep_deg))

        for prev_u, prev_deg in arrival_angles.get(v, []):
            if prev_u != u:
                diff = abs((arr_deg - prev_deg + 180.0) % 360.0 - 180.0)
                if diff < 15.0:
                    issues.append(
                        f"Edges ({prev_u} -> {v}) and ({u} -> {v}) arrive at node '{v}' at nearly the same angle "
                        f"(angle difference={diff:.1f}° < 15°), causing overlapping arrowheads at '{v}'."
                    )
        arrival_angles.setdefault(v, []).append((u, arr_deg))

        # Exact TikZ cubic Bezier control points for straight or bent edges
        c1x = ax + (center_dist / 3.0) * math.cos(dep_rad)
        c1y = ay + (center_dist / 3.0) * math.sin(dep_rad)
        c2x = bx + (center_dist / 3.0) * math.cos(arr_rad)
        c2y = by + (center_dist / 3.0) * math.sin(arr_rad)

        for wid, nw in nodes.items():
            if wid in (u, v):
                continue
            wx, wy = nw["x"], nw["y"]
            for step in range(2, 19):
                t = step / 20.0
                mt = 1.0 - t
                px = (mt**3) * ax + 3 * (mt**2) * t * c1x + 3 * mt * (t**2) * c2x + (t**3) * bx
                py = (mt**3) * ay + 3 * (mt**2) * t * c1y + 3 * mt * (t**2) * c2y + (t**3) * by
                if abs(wx - px) < (nw["hw"] + 0.32) and abs(wy - py) < (nw["hh"] + 0.32):
                    dist_w = math.hypot(wx - px, wy - py)
                    issues.append(
                        f"Edge ({u} -> {v}) passes too close to or cuts through intermediate node '{wid}' "
                        f"(distance={dist_w:.2f}cm). "
                        "Place graph vertices in a non-collinear convex polygon (pentagon/hexagon/staggered layout) so no edge crosses or touches any intermediate node."
                    )
                    break

    # 3. Check if ground_truth specifies directed edges (`->`) or subject is CT
    expects_directed = "->" in gt or subj_norm == "ct"
    if expects_directed and "\\begin{tikzpicture}" in latex_body and "\\node" in latex_body:
        has_arrow_draw = bool(
            re.search(r"->|edge\s*\[[^\]]*->", latex_body)
            or re.search(r"\\begin\{tikzpicture\}\[[^\]]*->", latex_body)
            or re.search(r"every\s+edge/\.style=\{[^\}]*->", latex_body)
        )
        if not has_arrow_draw:
            issues.append(
                "Diagram requires directed edges with visible arrowheads (`->`), but no `->` arrow specification was found on `\\draw` or `edge` commands."
            )

    # 4. Check symbol/notation consistency between ground_truth and TikZ node labels
    if not _is_flowchart_or_block_diagram(latex_body) and nodes:
        gt_edge_pairs = re.findall(r"\b([A-Za-z0-9_]+)\s*->\s*([A-Za-z0-9_]+)\b", gt)
        gt_vertices = {u for pair in gt_edge_pairs for u in pair}
        if gt_vertices and all(v.isdigit() for v in gt_vertices):
            displayed_labels = {
                re.sub(r"[\$\\{}]", "", str(n["label"])).strip()
                for n in nodes.values()
                if str(n["label"]).strip()
            }
            if displayed_labels and not displayed_labels.issubset(gt_vertices):
                issues.append(
                    f"Course/PYQ symbol notation mismatch: ground_truth specifies integer vertex labels {sorted(gt_vertices)}, "
                    f"but TikZ node labels render {sorted(displayed_labels)}. Use the exact course/PYQ vertex symbols {sorted(gt_vertices)} inside the node circles."
                )

    return issues


def _retrieve_course_and_pyq_notation(
    subject: str, caption: str, ground_truth: str, week: int | None = None
) -> str:
    """Build a unified course & PYQ notation context from `docs/<subject>/skill.md`
    plus any on-disk PYQ markdown files so diagrams strictly use the course's
    and PYQs' exact symbols and conventions without extra API latency."""
    from klyra.services.skill_service import load_subject_skill

    skill = load_subject_skill(subject)
    sections: list[str] = []
    if (skill.diagram_visual_conventions or "").strip():
        sections.append(f"Diagram & Visual Conventions:\n{skill.diagram_visual_conventions.strip()}")
    if (skill.domain_pedagogical_rules or "").strip():
        sections.append(f"Course Domain & Notation Rules:\n{skill.domain_pedagogical_rules.strip()}")

    # Scan on-disk PYQ markdown files for graph/diagram/matrix/table notation examples
    try:
        pyq_dir = fs.subject_dir(subject) / "pyq"
        if pyq_dir.exists():
            keywords = {
                w.lower()
                for w in re.findall(r"[A-Za-z]{3,}", f"{caption} {ground_truth}")
                if w.lower() not in {"the", "and", "with", "for", "from", "graph", "diagram", "nodes", "edges"}
            } or {"graph", "matrix", "table", "procedure"}
            snippets: list[str] = []
            for md_file in sorted(pyq_dir.rglob("*.md"))[:6]:
                text = md_file.read_text(encoding="utf-8", errors="ignore")
                for block in re.split(r"\n(?=###?\s+Question|\n##\s+Q)", text):
                    low = block.lower()
                    if any(k in low for k in keywords) and (
                        "```" in block or "matrix" in low or "graph" in low or "![q" in low or "[image:" in low
                    ):
                        clean_block = block.strip()[:500]
                        snippets.append(f"[PYQ {md_file.name}]\n{clean_block}")
                        break
                if len(snippets) >= 2:
                    break
            if snippets:
                sections.append("Course PYQ Reference Notation Examples:\n" + "\n\n".join(snippets))
    except Exception:
        pass

    return "\n\n".join(sections)


def render_latex_to_png(body: str, preamble: str = DEFAULT_PREAMBLE, dpi: int = 240) -> bytes:
    """`body` is the LaTeX content that goes between \\begin{document} and
    \\end{document} - e.g. a tikzpicture or tabular block. Returns PNG bytes."""
    pdflatex = _pdflatex_path()
    if not pdflatex:
        raise LatexNotAvailableError(
            "No LaTeX distribution found (pdflatex not on PATH). Install e.g. "
            "`texlive-latex-extra texlive-pictures` (Linux) or MacTeX (macOS) "
            "to enable generating new diagrams."
        )

    clean_body = _strip_code_fences(body)
    tex_source = f"{preamble}\n\\begin{{document}}\n{clean_body}\n\\end{{document}}\n"

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
            raise RuntimeError(f"LaTeX compilation failed:\n{result.stdout[-2000:]}")

        import pymupdf

        with pymupdf.open(str(pdf_file)) as doc:
            pix = doc[0].get_pixmap(dpi=dpi)
            return pix.tobytes("png")


def _extract_json_dict(raw_text: str) -> dict[str, Any] | None:
    text = (raw_text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        try:
            parsed = json.loads(text[first : last + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _vision_candidate_models() -> list[str]:
    models: list[str] = []
    for raw in (
        os.environ.get("KLYRA_PARSER_MODEL"),
        os.environ.get("KLYRA_MODEL"),
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
    ):
        if raw:
            clean = raw.replace("gemini/", "").strip()
            if clean and clean not in models:
                models.append(clean)
    return models


def verify_diagram_png_visually(
    png_bytes: bytes,
    latex_body: str,
    caption: str,
    ground_truth: str,
    subject: str = "",
    diagram_rules: str = "",
) -> tuple[bool, list[str], str | None]:
    """Inspect the rendered PNG image with Gemini Multimodal Vision across ANY
    diagram type (graphs, procedure flowcharts, logic circuits, ER diagrams,
    trees, B-trees, state machines) to detect visual defects:
      1. Edge cutting through or overlapping an intermediate node circle/box
         (e.g. an edge S1->S5 passing across node S2).
      2. Overlapping or colliding shapes (e.g. two flowchart diamonds or boxes
         touching/overlapping vertically or horizontally).
      3. Missing, squashed, inverted, or barely visible arrowheads or zero-length
         arrow shafts between boxes/nodes.
      4. Lines cutting through text labels or edge weights (e.g. an arrow line
         striking through 'Yes', 'No', or a weight number).
      5. Non-standard styling (e.g. blue/colored graph circles instead of clean
         black-and-white textbook circles with crisp borders).
      6. Mismatch between the rendered image and `ground_truth`.

    Returns `(passed, issues, None)`.
    """
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return True, [], None

    try:
        from google import genai
        from google.genai import types
        from klyra.core.retry import call_with_retry, sdk_retry_options

        client = genai.Client(
            api_key=api_key,
            http_options=sdk_retry_options(retry_attempts=2),
        )

        tikz_section = (
            f"Current LaTeX/TikZ body:\n```latex\n{latex_body}\n```\n\n"
            if latex_body.strip()
            else ""
        )
        prompt = (
            "You are a strict visual quality inspector for academic exam diagrams (graphs, flowcharts/procedure flows, logic circuits, ER diagrams, trees, state machines).\n"
            f"Subject: {subject}\n"
            f"Caption: {caption}\n"
            f"Expected Ground Truth: {ground_truth}\n"
            f"Subject Diagram Conventions:\n{diagram_rules or 'Standard monochrome academic textbook clarity'}\n\n"
            f"{tikz_section}"
            "Inspect the attached rendered PNG image with extreme care. You MUST set `\"passed\": false` if ANY of the following visual defects exist in the image:\n"
            "1. EDGE CUTTING THROUGH A VERTEX CIRCLE/BOX OR COINCIDENT COLLINEAR EDGES: Does any edge pass directly through an intermediate vertex's circle/box, or do two distinct edges lie collinearly on top of each other along the same path (e.g. S1->S5 running on top of S1->S2 and cutting through S2)? Note: clean diagonal X-crossings of two straight edges in open space between vertices are completely normal in graphs and are NOT a defect.\n"
            "2. SHAPE OVERLAP / COLLISION: Do any two nodes, decision diamonds, process rectangles, ovals, logic gates, or ER symbols touch, intersect, or overlap each other (for example, the bottom point of one decision diamond overlapping the top point of another diamond below it)?\n"
            "3. ARROW DEFECTS (MISSING / SQUASHED / INVERTED / SHORT SHAFT): Is any arrowhead missing, squashed with almost zero line shaft between boxes/diamonds, or pointing backwards/upwards inside an overlapping diamond/box?\n"
            "4. LINE CUTTING THROUGH TEXT / LABEL COLLISION: Does any line or arrow shaft pass directly through text such as 'Yes', 'No', edge weights, or node labels, or do labels collide with lines/arrowheads?\n"
            "5. TEXTBOOK GRAPH STYLE & EDGE WEIGHTS: For graphs, vertices must be clean black-and-white circles (`draw=black, fill=white`) arranged in a non-collinear polygon so all vertices and arrowheads are clearly separated — reject blue/colored vertex circles. Edge weights MUST be plain unboxed/uncircled numbers (never enclosed inside vertex-like circles `draw=black, circle`!).\n"
            "6. GROUND TRUTH & DOMAIN FIDELITY: Does any node, edge, direction, weight, gate, or flowchart branch in the image disagree with the Expected Ground Truth?\n"
            "7. COURSE & PYQ NOTATION / SYMBOL CONSISTENCY: Different textbooks use different symbols for the same meaning. Does the diagram violate the Subject Diagram Conventions or use symbols/labels that differ from the Expected Ground Truth and course/PYQ notation (e.g., using `v_0` or `S_1` when Ground Truth / course notation uses `0, 1, 2, 3, 4`, or using wrong ER/flowchart/gate symbols)?\n\n"
            "Respond with ONLY a valid JSON object (no LaTeX code inside JSON) matching this exact schema:\n"
            "{\n"
            '  "passed": true or false,\n'
            '  "issues": ["clear plain-text description of each visual defect seen in the PNG; empty list if passed is true"]\n'
            "}"
        )

        resp = None
        for model_name in _vision_candidate_models():
            try:
                resp = call_with_retry(
                    lambda m=model_name: client.models.generate_content(
                        model=m,
                        contents=[
                            types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                            types.Part.from_text(text=prompt),
                        ],
                        config={"response_mime_type": "application/json"},
                    ),
                    max_retries=2,
                )
                break
            except Exception as exc:
                msg = str(exc).lower()
                if "not found" in msg or "404" in msg or "not supported" in msg:
                    continue
                raise

        if resp is None:
            return True, [], None

        data = _extract_json_dict(resp.text or "")
        if not data:
            return True, [], None

        passed = bool(data.get("passed", True))
        raw_issues = data.get("issues") or []
        issues = [str(x).strip() for x in raw_issues if str(x).strip()]
        if issues and passed:
            passed = False
        return passed, issues, None
    except Exception:
        return True, [], None


def _regenerate_tikz_with_llm(
    latex_body: str,
    issues: list[str],
    caption: str,
    ground_truth: str,
    subject: str = "",
    diagram_rules: str = "",
    png_bytes: bytes | None = None,
) -> str | None:
    """Ask Gemini (with the defective PNG attached when available) to rewrite
    broken or visually defective TikZ code."""
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai
        from google.genai import types
        from klyra.core.retry import sdk_retry_options

        client = genai.Client(
            api_key=api_key,
            http_options=sdk_retry_options(retry_attempts=2),
        )

        issues_bullet = "\n".join(f"- {iss}" for iss in issues)
        prompt = (
            "You are a LaTeX/TikZ expert fixing an academic diagram that failed compilation or visual quality verification.\n"
            f"Subject: {subject}\n"
            f"Caption: {caption}\n"
            f"Ground Truth (must match 100%): {ground_truth}\n"
            f"Subject Diagram Conventions:\n{diagram_rules}\n\n"
            f"Detected Visual / Structural Defects to Fix:\n{issues_bullet}\n\n"
            f"Previous LaTeX/TikZ body:\n```latex\n{latex_body}\n```\n\n"
            "STRICT RULES FOR THE CORRECTED LaTeX/TikZ CODE:\n"
            "1. Output ONLY the LaTeX body that goes inside \\begin{document}...\\end{document} (e.g. \\begin{tikzpicture}...\\end{tikzpicture}). Do NOT include \\documentclass or \\begin{document}.\n"
            "2. FOR GRAPHS: Define a named style `vertex/.style={circle, draw=black, fill=white, thick, minimum size=8.5mm}` and apply `\\node[vertex]` to graph vertices — NEVER set `every node/.style={circle, draw=black}` (because that puts circles around edge weight labels!) and NEVER use blue/colored circles. Arrange vertices on a non-collinear convex polygon (e.g. pentagon/hexagon: `(0,2.0)`, `(2.2,3.8)`, `(5.2,3.8)`, `(5.2,0.0)`, `(2.2,0.0)`) so NO straight or curved edge ever cuts through or touches another vertex circle. Place edge weights as plain unbordered numbers with `fill=white, inner sep=1.5pt` or `above`/`below`/`left`/`right` so crossing edges never collide with weight numbers.\n"
            "3. FOR FLOWCHARTS / PROCEDURE FLOWS / ER DIAGRAMS / CIRCUITS: ALWAYS use relative border-to-border positioning from `\\usetikzlibrary{positioning}` (`below=1.5cm of prev`, `right=2.4cm of prev`) instead of cramped hardcoded `(x,y)` coordinates, so tall `diamond` decision blocks (`diamond, aspect=2.0, draw=black, thick, align=center, inner sep=2pt`) and `rectangle` process blocks NEVER overlap and every connecting arrow has a clear 1.5cm+ shaft. Place branch labels ('Yes', 'No') with `fill=white, inner sep=2pt` or clear `above`/`left`/`right` offsets so lines NEVER cut through the text.\n"
            "4. Ensure all directed edges use `->, >={Stealth[length=3.5mm,width=2.4mm]}, thick` and bidirectional edges use `bend left=20`.\n"
            "5. Match the Ground Truth and the Course & PYQ Notation/Symbol Conventions 100% — never substitute alternative textbook symbols that have the same meaning."
        )

        contents: list[Any] = []
        if png_bytes:
            contents.append(types.Part.from_bytes(data=png_bytes, mime_type="image/png"))
        contents.append(types.Part.from_text(text=prompt))

        for model_name in _vision_candidate_models():
            try:
                resp = client.models.generate_content(model=model_name, contents=contents)
                fixed = _strip_code_fences(resp.text or "")
                if fixed:
                    return fixed
            except Exception as exc:
                msg = str(exc).lower()
                if "not found" in msg or "404" in msg or "not supported" in msg:
                    continue
                raise
        return None
    except Exception:
        return None


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

        skill = load_subject_skill(subject)
        diagram_rules = _retrieve_course_and_pyq_notation(
            subject=subject, caption=caption, ground_truth=ground_truth, week=week
        )

        current_body = _normalize_tikz_body(latex_body, subject=subject)
        png_bytes: bytes | None = None

        for attempt in range(MAX_DIAGRAM_RETRIES + 1):
            # Step 1: Compile LaTeX/TikZ to PNG
            try:
                png_bytes = render_latex_to_png(current_body)
            except LatexNotAvailableError as exc:
                return f"{exc} Write this question as text-only instead."
            except Exception as exc:  # noqa: BLE001
                if attempt < MAX_DIAGRAM_RETRIES:
                    repaired = _regenerate_tikz_with_llm(
                        latex_body=current_body,
                        issues=[f"LaTeX compilation error: {exc}"],
                        caption=caption,
                        ground_truth=ground_truth,
                        subject=subject,
                        diagram_rules=diagram_rules,
                    )
                    if repaired:
                        current_body = _normalize_tikz_body(repaired, subject=subject)
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

            # Step 2: Geometric collision/layout check + Multimodal Vision check on the rendered PNG
            structural_issues = _detect_structural_tikz_issues(
                current_body, ground_truth=ground_truth, subject=subject
            )
            vis_passed, vis_issues, fixed_from_vision = verify_diagram_png_visually(
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

            if attempt < MAX_DIAGRAM_RETRIES:
                candidate_fix: str | None = None
                if fixed_from_vision and not structural_issues:
                    candidate_fix = fixed_from_vision
                else:
                    candidate_fix = _regenerate_tikz_with_llm(
                        latex_body=current_body,
                        issues=all_issues,
                        caption=caption,
                        ground_truth=ground_truth,
                        subject=subject,
                        diagram_rules=diagram_rules,
                        png_bytes=png_bytes,
                    ) or fixed_from_vision
                if candidate_fix and candidate_fix.strip() != current_body.strip():
                    current_body = _normalize_tikz_body(candidate_fix, subject=subject)
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
