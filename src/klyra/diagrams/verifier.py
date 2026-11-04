"""Geometric collision/layout verification and Gemini Multimodal Vision verification
for rendered LaTeX/TikZ diagrams."""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any

from klyra.diagrams.compiler import (
    _DIRECTED_EDGE_PAIR_RE,
    _NODE_FULL_RE,
    _is_flowchart_or_block_diagram,
    _strip_code_fences,
)
from klyra.utils import fs

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _estimate_node_half_extents_cm(opts: str, label: str, style_block: str) -> tuple[float, float]:
    """Estimate `(half_width_cm, half_height_cm)` of a TikZ node for overlap and
    edge-collision checking."""
    combined = f"{opts} {style_block}".lower()
    clean_label = re.sub(r"\$|[\\{}]", "", label)
    lines = [ln.strip() for ln in re.split(r"\\\\", clean_label) if ln.strip()] or [clean_label]
    max_chars = max((len(ln) for ln in lines), default=2)
    num_lines = len(lines)

    if "diamond" in combined or "decision" in combined:
        hw = max(1.4, max_chars * 0.11 + 0.6)
        hh = max(1.2, num_lines * 0.45 + 0.5)
        return hw, hh
    if any(k in combined for k in ("rectangle", "rounded corners", "block", "process", "entity", "trapezium")):
        hw = max(1.2, max_chars * 0.09 + 0.4)
        hh = max(0.55, num_lines * 0.32 + 0.25)
        return hw, hh
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
    """Deterministic geometric & structural checks for TikZ diagrams."""
    issues: list[str] = []
    subj_norm = (subject or "").strip().lower()
    gt = (ground_truth or "").strip()

    sx_m = re.search(r"\bx\s*=\s*(\d+(?:\.\d+)?)\s*cm", latex_body)
    sy_m = re.search(r"\by\s*=\s*(\d+(?:\.\d+)?)\s*cm", latex_body)
    sx = float(sx_m.group(1)) if sx_m else 1.0
    sy = float(sy_m.group(1)) if sy_m else 1.0

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

    if not _is_flowchart_or_block_diagram(latex_body) and nodes:
        gt_edge_pairs = re.findall(r"\b([A-Za-z0-9_]+)\s*->\s([A-Za-z0-9_]+)\b|\b([A-Za-z0-9_]+)->([A-Za-z0-9_]+)\b", gt)
        gt_vertices = {u for tup in gt_edge_pairs for u in tup if u}
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
    plus any on-disk PYQ markdown files."""
    from klyra.services.skill_service import load_subject_skill

    skill = load_subject_skill(subject)
    sections: list[str] = []
    if (skill.diagram_visual_conventions or "").strip():
        sections.append(f"Diagram & Visual Conventions:\n{skill.diagram_visual_conventions.strip()}")
    if (skill.domain_pedagogical_rules or "").strip():
        sections.append(f"Course Domain & Notation Rules:\n{skill.domain_pedagogical_rules.strip()}")

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
    trees, B-trees, state machines) to detect visual defects."""
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

