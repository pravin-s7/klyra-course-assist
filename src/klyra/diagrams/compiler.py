"""TikZ normalization and LaTeX -> PNG compilation via pdflatex + PyMuPDF."""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

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
    cross = abs((p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0]))
    max_side = max(
        math.hypot(p2[0] - p1[0], p2[1] - p1[1]),
        math.hypot(p3[0] - p2[0], p3[1] - p2[1]),
        math.hypot(p3[0] - p1[0], p3[1] - p1[1]),
        1e-6,
    )
    perp_h = cross / max_side
    return perp_h < 0.35


def _convex_polygon_coords(n: int) -> list[tuple[float, float]]:
    """Return textbook convex polygon coordinates (matching Image 3's pentagon style for n=5)."""
    if n == 5:
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
    cut through intermediate vertices or forcing ugly bent arcs), rearrange the
    vertices into a clean textbook convex polygon and remove unnecessary `bend`
    from unidirectional edges."""
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
        if cleaned_opts:
            res = re.sub(r"(?:edge|to)\s*\[[^\]]*\]", f"edge[{cleaned_opts}]", full, count=1)
        else:
            res = re.sub(r"(?:edge|to)\s*\[[^\]]*\]", "--", full, count=1)
        if "pos=" not in res and "node[" in res:
            res = res.replace("node[", "node[pos=0.35, ", 1)
        return res

    body = _DIRECTED_EDGE_PAIR_RE.sub(_straighten_unidirectional, body)
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
    """Normalize TikZ code for graphs, flowcharts, circuits, ER diagrams, and trees."""
    body = _strip_code_fences(latex_body)
    if "\\begin{tikzpicture}" not in body:
        return body

    body = re.sub(
        r">=\s*(?:latex|stealth|to)\b",
        r">={Stealth[length=3.5mm,width=2.4mm]}",
        body,
        flags=re.IGNORECASE,
    )

    body = re.sub(r"\bdraw\s*=\s*blue(?:![0-9]+)?", "draw=black", body, flags=re.IGNORECASE)
    body = re.sub(r"\bfill\s*=\s*blue![0-9]+", "fill=white", body, flags=re.IGNORECASE)
    body = re.sub(r"\btext\s*=\s*blue(?:![0-9]+)?", "text=black", body, flags=re.IGNORECASE)

    body = _decollinearize_graph_nodes(body)

    def _fix_deprecated_of(m: re.Match[str]) -> str:
        direction = m.group(1).lower()
        target = m.group(2)
        dist = "1.6cm" if direction in ("below", "above") else "2.2cm"
        return f"{direction}={dist} of {target}"

    body = _DEPRECATED_OF_RE.sub(_fix_deprecated_of, body)

    def _fix_short_pos(m: re.Match[str]) -> str:
        direction = m.group(1).lower()
        val = _to_cm(float(m.group(2)), m.group(3))
        target = m.group(4)
        min_cm = 1.4 if direction in ("below", "above") else 1.8
        if val < min_cm:
            return f"{direction}={min_cm:.1f}cm of {target}"
        return m.group(0)

    body = _SHORT_POS_RE.sub(_fix_short_pos, body)

    body = re.sub(
        r"\bdiamond\b(?![^\]]*aspect)",
        "diamond, aspect=2.0, inner sep=2pt",
        body,
    )

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

